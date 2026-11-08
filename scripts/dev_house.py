"""Build a realistic demo house for screenshots and manual testing.

Uses the real flow: the simulated node boots and announces its devices, then an
admin renames/rooms them and adds two devices that are not on the node yet (so
the UI shows both online and offline states).

    python scripts/dev_server.py        # terminal 1
    python scripts/dev_house.py         # terminal 2
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
PASSWORD = "test-password"

#: uid -> (room, name) for devices the simulated node announces
ROOMS = {
    "strip-1": ("Office", "Desk strip"),
    "relay-1": ("Utility", "Pump"),
    "temp-1": ("Living room", "Room temperature"),
}

#: Devices created on the hub only (their board is not powered yet).
OFFLINE = [
    ("relay", "garage-light", "Garage", "Garage light", {"pin": 5}),
    ("button", "hall-button", "Hallway", "Hall button", {"pin": 9}),
]


def wait_for(condition, timeout=25.0, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=10) as client:
        response = client.post("/api/login", json={"password": PASSWORD})
        if response.status_code != 200:
            print("login failed:", response.status_code, response.text)
            return 1
        client.post("/api/enroll", json={"seconds": 300})
        token = client.post("/api/nodes/esp32-desk/token", json={}).json()["token"]

    node = subprocess.Popen(
        [sys.executable, "-m", "tools.fake_node", "--server", BASE,
         "--node", "esp32-desk", "--token", token],
        cwd=str(ROOT),
    )
    try:
        with httpx.Client(base_url=BASE, timeout=10) as client:
            client.post("/api/login", json={"password": PASSWORD})

            def devices():
                return client.get("/api/devices").json()["devices"]

            if not wait_for(lambda: len(devices()) >= 3):
                print("node never announced its devices")
                return 1

            for uid, (room, name) in ROOMS.items():
                client.patch("/api/devices/" + uid, json={"room": room, "name": name})

            for type_id, uid, room, name, config in OFFLINE:
                client.post("/api/devices", json={
                    "type": type_id, "uid": uid, "name": name, "room": room,
                    "node_id": "esp32-desk", "config": config,
                })

            # A strip that is on, and a relay that is off.
            client.post("/api/devices/strip-1/props", json={"props": {"power": True, "brightness": 72}})
            time.sleep(1.0)
            print("demo house ready: %d devices" % len(devices()))
            print("  open %s/ and log in with %s" % (BASE, PASSWORD))
        node.wait()
    except KeyboardInterrupt:
        node.terminate()
    finally:
        if node.poll() is None:
            node.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
