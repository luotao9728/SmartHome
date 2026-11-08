"""End-to-end rehearsal of the user's actual hardware.

Fresh hub -> enrol an ESP32-C6 that owns a 24V PWM LED strip on AOD4184 ->
control power, brightness, fades and frequency -> yank the link mid-fade ->
reconnect and confirm the strip comes back exactly as intended.

Everything here is async on purpose: the simulated node and the HTTP client
share one event loop, exactly like a real board running while you click.

    SMARTHOME_PASSWORD=... python scripts/rehearse.py http://pi:8080
    python scripts/rehearse.py http://pi:8080 "the admin password"
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "SMARTHOME_URL", "http://127.0.0.1:8080")


def _password() -> str:
    """Password from argv, the environment, or an interactive prompt."""
    if len(sys.argv) > 2:
        return sys.argv[2]
    from_env = os.environ.get("SMARTHOME_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass("hub admin password: ")


PASSWORD = _password()
NODE = "esp32-desk"

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, condition, detail: str = "") -> None:
    CHECKS.append((label, bool(condition), detail))
    print(("  ok   " if condition else "  FAIL ") + label + ((" " + detail) if detail else ""))


async def wait(predicate, label: str, timeout: float = 15.0):
    """Poll an async predicate without blocking the event loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.05)
    raise SystemExit("FAILED (timeout): %s" % label)


async def main() -> int:
    sys.path.insert(0, "micropython")
    from shnode.agent import Agent
    from shnode.devices.pwm_led_strip import PwmLedStrip

    async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
        assert (await client.post("/api/login", json={"password": PASSWORD})).status_code == 200
        await client.post("/api/enroll", json={"seconds": 120})

        async def devices():
            return (await client.get("/api/devices")).json()["devices"]

        async def device():
            found = [d for d in await devices() if d["uid"] == "desk-strip"]
            return found[0] if found else None

        async def set_props(props):
            response = await client.post("/api/devices/desk-strip/props", json={"props": props})
            assert response.status_code == 200, response.text

        agent = Agent({"server": BASE, "node_id": NODE, "token": "",
                       "heartbeat_s": 2, "report_s": 10})
        strip = PwmLedStrip("desk-strip", {"pin": 4}, name="Desk strip")
        strip.state["frequency_hz"] = 1000
        agent.add(strip)
        task = asyncio.create_task(agent.main())

        try:
            # 1. enrolment + auto-registration (also works on a hub that already
            #    knows this device from a previous run)
            await wait(device, "device registration")
            await wait(lambda: agent.online, "board online")
            initial = await device()
            await wait(lambda: initial["online"] is True, "device online")
            check("strip auto-registered from the board", initial["online"] is True)
            check("board received a token", agent.token.startswith("shd_"))
            check("hardware config preserved", initial["config"].get("pin") == 4,
                  str(initial["config"]))
            check("hub fills the rest from the manifest",
                  set(initial["config"]) >= {"pin", "gamma", "max_duty"})

            # 2. power + brightness through gamma
            await set_props({"power": True, "brightness": 40, "fade_ms": 0})
            await wait(lambda: strip.snapshot()["power"] is True, "power on")
            await wait(lambda: strip._current_duty > 0, "duty raised")
            check("gamma applied at 40 %", strip._current_duty == strip._table[40],
                  "duty=%d" % strip._current_duty)
            current = await device()
            check("hub sees it as reported", current["reported"]["power"] is True)
            check("nothing pending", current["pending"] == [])
            check("no error surfaced", current["error"] == "")

            # 3. a burst of slider moves, like a real drag
            for percent in (45, 50, 55, 60, 65, 70, 75, 80):
                await set_props({"brightness": percent})
            await wait(lambda: strip.snapshot()["brightness"] == 80, "drag settles at 80")
            current = await device()
            check("rapid drags converge on the last value",
                  strip.snapshot()["brightness"] == 80 and current["reported"]["brightness"] == 80)

            # 4. fading
            await set_props({"brightness": 10, "fade_ms": 300})
            await asyncio.sleep(0.08)
            mid = strip._current_duty
            await asyncio.sleep(0.8)
            check("fade starts above the target", mid > strip._table[10],
                  "mid=%d target=%d" % (mid, strip._table[10]))
            check("fade lands exactly on target", strip._current_duty == strip._table[10])

            # 5. runtime frequency change
            await set_props({"frequency_hz": 2000})
            await wait(lambda: strip._pwm.freq() == 2000, "frequency change")
            check("PWM retuned at runtime", strip._pwm.freq() == 2000)

            # 6. an action round trip
            response = await client.post("/api/devices/desk-strip/action",
                                         json={"action": "identify"})
            check("action accepted", response.status_code == 200)
            await asyncio.sleep(1.6)
            check("state restored after identify", strip.snapshot()["brightness"] == 10)

            # 7. cut the link, reset the hardware behind the hub's back
            await set_props({"power": True, "brightness": 55, "fade_ms": 0})
            await wait(lambda: strip.snapshot()["brightness"] == 55, "applied before the cut")
            await agent.ws.close()
            await wait(lambda: agent.online is False, "link down")
            health = (await client.get("/healthz")).json()
            check("hub marks the node offline", health["nodes_online"] == 0)

            strip.state["power"] = False
            strip.state["brightness"] = 100
            strip._write_duty(0)

            await wait(lambda: agent.online is True, "board reconnects", timeout=40)
            await wait(lambda: strip.snapshot()["brightness"] == 55,
                       "desired state re-applied", timeout=20)
            check("reconnect restores the user's settings",
                  strip.snapshot()["power"] is True and strip.snapshot()["brightness"] == 55)
            check("hub agrees after reconnect", (await device())["reported"]["brightness"] == 55)

            # 8. a change made while offline is queued, then applied
            await agent.ws.close()
            await asyncio.sleep(0.8)
            await set_props({"brightness": 15})
            queued = await device()
            check("offline change is queued, not lost",
                  queued["desired"]["brightness"] == 15 and "offline" in queued["error"].lower())
            await wait(lambda: strip.snapshot()["brightness"] == 15,
                       "queued change applied on reconnect", timeout=40)
            check("queued change applied after reconnect", strip.snapshot()["brightness"] == 15)
        finally:
            agent.stop()
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            for item in agent.devices:
                await item.stop()

    failed = [label for label, ok, _ in CHECKS if not ok]
    print("\n%d/%d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
