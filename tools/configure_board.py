"""Rewrite /config.json and /devices.json on a board already running MicroPython.

No flashing, no erasing -- it just overwrites the two JSON files over the
existing MicroPython session, then reboots the board.  Use it when the firmware
is fine but the network or device settings are wrong (the usual case after a
board ends up on the wrong subnet).

    python tools/configure_board.py                          # show current settings
    python tools/configure_board.py --server http://10.0.0.105:8080 \
        --wifi-ssid "MyNet" --wifi-pass "secret" --write

    python tools/configure_board.py --pin 15 --max-duty 30 --min-duty 9 \
        --gamma 1.4 --smoothing 0.18 --write

Values not given keep whatever the board already has.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SERVER = "http://10.0.0.105:8080"


def say(message: str) -> None:
    print(message, flush=True)


def mpremote(port: str, *args: str, timeout: float = 60.0) -> Tuple[int, str]:
    command = [sys.executable, "-m", "mpremote", "connect", port] + list(args)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def read_json(port: str, remote: str) -> Tuple[Optional[Any], str]:
    """Read and parse a JSON file from the board."""
    code, output = mpremote(
        port, "exec",
        "print('BEGIN');print(open(%r).read());print('END')" % remote,
    )
    if code != 0:
        return None, output.strip() or "cannot read %s" % remote
    if "BEGIN" not in output:
        return None, output.strip() or "no output for %s" % remote
    body = output.split("BEGIN", 1)[1].split("END", 1)[0].strip()
    try:
        return json.loads(body), ""
    except ValueError as exc:
        return None, "invalid JSON in %s: %s" % (remote, exc)


def write_json(port: str, remote: str, payload: Any) -> Tuple[bool, str]:
    """Copy a JSON file to the board via a local temp file."""
    staging = Path(tempfile.mkdtemp(prefix="shcfg-"))
    local = staging / Path(remote).name
    try:
        local.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        code, output = mpremote(port, "fs", "cp", str(local), ":" + Path(remote).name,
                                timeout=120.0)
        return code == 0, output.strip()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def current_network(port: str) -> str:
    code, output = mpremote(
        port, "exec",
        "import network;w=network.WLAN(network.STA_IF);"
        "print('NET',w.isconnected(),w.ifconfig(),w.status())",
    )
    if code != 0 or "NET" not in output:
        return "  (could not read the network state)"
    line = [piece for piece in output.splitlines() if "NET" in piece][0]
    connected = "yes" if "True" in line else "no"
    start, end = line.find("("), line.find(")")
    detail = line[start + 1:end] if start >= 0 and end > start else ""
    return "  connected: %-3s  %s" % (connected, detail)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite the config files on a board running MicroPython")
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--server", default=None,
                        help="hub URL (default: %s)" % DEFAULT_SERVER)
    parser.add_argument("--wifi-ssid", default=None)
    parser.add_argument("--wifi-pass", default=None)
    parser.add_argument("--node", default=None, help="node id")
    parser.add_argument("--token", default=None, help="device token")
    parser.add_argument("--uid", default=None, help="device uid")
    parser.add_argument("--name", default=None, help="device display name")
    parser.add_argument("--type", default=None, help="device type id")
    parser.add_argument("--pin", type=int, default=None)
    parser.add_argument("--min-duty", type=float, default=None)
    parser.add_argument("--max-duty", type=int, default=None)
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--smoothing", type=float, default=None)
    parser.add_argument("--interval-ms", type=int, default=None)
    parser.add_argument("--frequency", type=int, default=None)
    parser.add_argument("--write", action="store_true",
                        help="actually write; without it, only show the result")
    parser.add_argument("--no-reset", action="store_true")
    args = parser.parse_args(argv)

    say("current settings on %s" % args.port)
    say("  network:")
    say(current_network(args.port))

    config, problem = read_json(args.port, "/config.json")
    if config is None:
        say("")
        say("cannot read /config.json: %s" % problem)
        say("Is MicroPython running and the port correct? Check with:")
        say("  python -m mpremote connect %s exec \"print('ok')\"" % args.port)
        return 2
    devices, device_problem = read_json(args.port, "/devices.json")
    if devices is None:
        say("  note: %s (will create a new one)" % device_problem)
        devices = []

    say("  server:   %s" % config.get("server", ""))
    say("  node:     %s" % config.get("node_id", ""))
    say("  wifi:     %s" % config.get("wifi_ssid", ""))
    say("  token:    %s" % ("set" if config.get("token") else "none"))
    for entry in devices if isinstance(devices, list) else []:
        say("  device:   %s (%s) pin=%s" % (
            entry.get("uid"), entry.get("type"), (entry.get("config") or {}).get("pin")))

    # -- apply the overrides --------------------------------------------- #
    if args.server:
        config["server"] = args.server.rstrip("/")
    if args.wifi_ssid is not None:
        config["wifi_ssid"] = args.wifi_ssid
    if args.wifi_pass is not None:
        config["wifi_pass"] = args.wifi_pass
    if args.node is not None:
        config["node_id"] = args.node
    if args.token is not None:
        config["token"] = args.token

    if isinstance(devices, list) and devices:
        entry = devices[0]
    else:
        entry = {"type": "pwm_led_strip", "uid": "bed-strip",
                 "name": "Light", "config": {}}
        devices = [entry]
    if args.type:
        entry["type"] = args.type
    if args.uid:
        entry["uid"] = args.uid
    if args.name:
        entry["name"] = args.name
    settings: Dict[str, Any] = entry.setdefault("config", {})
    if args.pin is not None:
        settings["pin"] = args.pin
    if args.min_duty is not None:
        settings["min_duty"] = args.min_duty
    if args.max_duty is not None:
        settings["max_duty"] = args.max_duty
    if args.gamma is not None:
        settings["gamma"] = args.gamma
    if args.smoothing is not None:
        settings["smoothing"] = args.smoothing
    if args.interval_ms is not None:
        settings["update_interval_ms"] = args.interval_ms
    if args.frequency is not None:
        settings["frequency_hz"] = args.frequency

    say("")
    say("new settings")
    say("  server:   %s" % config.get("server", ""))
    say("  node:     %s" % config.get("node_id", ""))
    say("  wifi:     %s" % config.get("wifi_ssid", ""))
    say("  device:   %s pin=%s" % (entry.get("uid"), settings.get("pin")))
    say("  config:   %s" % json.dumps(settings, sort_keys=True))

    if not args.write:
        say("")
        say("nothing written. add --write to apply.")
        return 0

    say("")
    ok, output = write_json(args.port, "/config.json", config)
    if not ok:
        say("failed to write /config.json: %s" % output)
        return 3
    say("  wrote /config.json")

    ok, output = write_json(args.port, "/devices.json", devices)
    if not ok:
        say("failed to write /devices.json: %s" % output)
        return 3
    say("  wrote /devices.json")

    if not args.no_reset:
        mpremote(args.port, "reset", timeout=30.0)
        say("  rebooted")

    say("")
    say("done. To watch it start:")
    say("  python -m mpremote connect %s repl" % args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
