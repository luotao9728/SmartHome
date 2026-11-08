"""Robustness layer for the node: watchdog, power-fail recovery, safe Wi-Fi.

Copy to the board as ``/boot.py`` if you want everything wired up automatically::

    mpremote cp micropython/boot.py :boot.py
    mpremote cp micropython/main.py :main.py
    mpremote cp -r micropython/shnode :shnode

``main.py`` (not this file) decides *which* devices exist.
"""

import gc
import sys

#: Seconds the watchdog allows between feeds.
WATCHDOG_TIMEOUT_S = 60

#: Feed the hardware watchdog from the agent's main loop.
ENABLE_WATCHDOG = True


def _machine_watchdog():
    """Return a feed function and a cleanup, or ``(None, None)`` off-device."""
    try:
        import machine
    except ImportError:
        return None, None

    dog = None
    try:
        if hasattr(machine, "WDT"):
            dog = machine.WDT(timeout=WATCHDOG_TIMEOUT_S * 1000)
            return (lambda: dog.feed()), None
        if hasattr(machine, "wdt"):  # older ports
            machine.wdt(timeout=WATCHDOG_TIMEOUT_S * 1000)
            return (lambda: None), None
    except Exception:  # noqa: BLE001
        pass
    return None, None


def feed_watchdog(feed) -> None:
    if feed is not None:
        try:
            feed()
        except Exception:  # noqa: BLE001
            pass


def safe_main() -> None:
    """Run ``main.start()`` with ram cleanup and a serial-safe error report."""
    try:
        import main

        main.start()
    except KeyboardInterrupt:
        print("stopped")
    except Exception as exc:  # noqa: BLE001
        print("fatal error in main:")
        try:
            sys.print_exception(exc)
        except Exception:  # noqa: BLE001
            print(repr(exc))
        gc.collect()


def boot() -> None:
    gc.collect()
    feed, _ = _machine_watchdog() if ENABLE_WATCHDOG else (None, None)
    try:
        from shnode import agent as agent_module

        agent_module.set_watchdog(feed)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    boot()
