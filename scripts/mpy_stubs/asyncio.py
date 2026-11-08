"""Minimal ``asyncio`` stand-in so the firmware can be imported by a bare
MicroPython build that ships without the ``asyncio`` module (such as the conda
package used by ``scripts/mpy_check.py --imports``).

It only needs to satisfy *import time* and the synchronous code paths the
firmware test exercises: real coroutines are created by the firmware itself, so
the stubs simply refuse to run them.
"""


class CancelledError(Exception):
    pass


class TimeoutError(Exception):
    pass


def sleep(*args, **kwargs):
    raise RuntimeError("stub asyncio: sleep() cannot run outside a real event loop")


def sleep_ms(*args, **kwargs):
    raise RuntimeError("stub asyncio: sleep_ms() cannot run outside a real event loop")


def create_task(coro, *args, **kwargs):
    raise RuntimeError("stub asyncio: create_task() needs a real event loop")


def ensure_future(coro, *args, **kwargs):
    raise RuntimeError("stub asyncio: ensure_future() needs a real event loop")


def run(coro):
    raise RuntimeError("stub asyncio: run() needs a real event loop")


class _Wait:
    def __await__(self):
        raise RuntimeError("stub asyncio")


def wait_for(awaitable, timeout):
    return _Wait()


def open_connection(*args, **kwargs):
    return _Wait()


def start_server(*args, **kwargs):
    return _Wait()
