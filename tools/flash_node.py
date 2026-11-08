#!/usr/bin/env python3
"""Provision and deploy the SmartHome node firmware over a serial port.

Uses ``mpremote`` (``pip install mpremote``) so it works on Windows, macOS and
Linux, and speaks the same protocol the boards speak at runtime.

    # one-time: copy the library and boot/main to the board
    python tools/flash_node.py --port COM5 --node esp32-desk --server http://192.168.1.20:8080

    # push a new firmware version later
    python tools/flash_node.py --port COM5 --only-files

    # ask the hub for a token first, then flash
    python tools/flash_node.py --port COM5 --node esp32-desk \\
        --server http://192.168.1.20:8080 --hub-password secret

The script writes ``/config.json`` on the device with Wi-Fi credentials, the
hub URL and the bearer token.  Credentials are never written to the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "micropython"

#: Files copied to the board root.
ROOT_FILES = ["boot.py", "main.py"]
#: Directories copied recursively.
DIRECTORIES = ["shnode"]


def find_mpremote() -> list[str]:
    exe = shutil.which("mpremote")
    if exe:
        return [exe]
    probe = subprocess.run([sys.executable, "-c", "import mpremote"],
                           capture_output=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", "mpremote"]
    raise SystemExit(
        "mpremote is not installed.\n"
        "  pip install mpremote\n"
        "or: python -m pip install --user mpremote"
    )


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("$ " + " ".join(str(part) for part in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        raise SystemExit("command failed with %d" % result.returncode)
    return result


def mpremote(args: list[str], port: str | None, *, check: bool = True) -> subprocess.CompletedProcess:
    base = find_mpremote()
    if port:
        base = base + ["connect", port]
    return run(base + args, check=check)


def detect_port() -> str | None:
    """Guess a serial port when the user did not pass one."""
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        return None
    patterns = ("303a", "2e8a", "1a86", "10c4", "0483")  # Espressif, RP2, CH34x, CP210x, STM
    for port in list_ports.comports():
        hwid = (port.hwid or "").lower()
        if any(pattern in hwid for pattern in patterns):
            return port.device
    return None


def fetch_token(server: str, node_id: str, password: str, username: str = "admin") -> str:
    import urllib.error
    import urllib.request

    login_url = server.rstrip("/") + "/api/login"
    # The username is required once the hub has more than one account; sending
    # it always is harmless (the hub ignores it when only one account exists).
    body = {"password": password}
    if username:
        body["username"] = username
    request = urllib.request.Request(
        login_url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise SystemExit("hub login failed: %s" % response.status)
    token_url = server.rstrip("/") + "/api/nodes/%s/token" % node_id
    request = urllib.request.Request(
        token_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    with opener.open(request, timeout=10) as response:
        payload = json.loads(response.read())
    return payload["token"]


def build(devices: str | None) -> Path:
    """Copy the firmware to a staging directory (optionally with a device list)."""
    staging = Path(tempfile.mkdtemp(prefix="shnode-stage-"))
    for name in ROOT_FILES:
        shutil.copy2(FIRMWARE / name, staging / name)
    for name in DIRECTORIES:
        shutil.copytree(FIRMWARE / name, staging / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.mpy"))
    if devices:
        text = (staging / "main.py").read_text(encoding="utf-8")
        marker = "DEVICES = ["
        start = text.index(marker)
        end = text.index("]", start) + 1
        replacement = "DEVICES = " + devices.strip()
        text = text[:start] + replacement + text[end:]
        (staging / "main.py").write_text(text, encoding="utf-8")
    return staging


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Flash a SmartHome node")
    parser.add_argument("--port", default=None, help="serial port, e.g. COM5 or /dev/ttyACM0")
    parser.add_argument("--node", default=None, help="node id (defaults to the board's unique id)")
    parser.add_argument("--server", default=os.environ.get("SMARTHOME_SERVER", ""),
                        help="hub URL, e.g. http://192.168.1.20:8080")
    parser.add_argument("--wifi-ssid", default=os.environ.get("SMARTHOME_WIFI_SSID", ""))
    parser.add_argument("--wifi-pass", default=os.environ.get("SMARTHOME_WIFI_PASS", ""))
    parser.add_argument("--token", default=os.environ.get("SMARTHOME_NODE_TOKEN", ""))
    parser.add_argument("--hub-password", default=None,
                        help="hub admin password; the script asks the hub for a device token")
    parser.add_argument("--hub-username", default="admin",
                        help="hub account to log in as (default: admin)")
    parser.add_argument("--devices", default=None,
                        help='JSON device list, e.g. \'[{"type":"relay","uid":"pump","config":{"pin":5}}]\'')
    parser.add_argument("--only-files", action="store_true", help="copy files, do not rewrite config.json")
    parser.add_argument("--factory", action="store_true", help="reboot and erase the stored token")
    parser.add_argument("--keep-staging", action="store_true")
    args = parser.parse_args(argv)

    if not FIRMWARE.is_dir():
        raise SystemExit("cannot find %s" % FIRMWARE)

    port = args.port or detect_port()
    if port:
        print("using port %s" % port)
    else:
        print("no serial port given; mpremote will auto-detect")

    if args.hub_password:
        if not args.node or not args.server:
            raise SystemExit("--hub-password needs --node and --server")
        args.token = fetch_token(args.server, args.node, args.hub_password,
                                 args.hub_username)
        print("fetched a device token for %s" % args.node)

    staging = build(args.devices)
    try:
        print("\n== copying firmware ==")
        # "fs" and the sub-command are separate argv entries: mpremote parses
        # them individually, so "fs cp" as one string is not a command.
        mpremote(["fs", "mkdir", ":"], port, check=False)
        for name in DIRECTORIES:
            mpremote(["fs", "mkdir", ":" + name], port, check=False)
            mpremote(["fs", "cp", "-r", str(staging / name), ":"], port)
        for name in ROOT_FILES:
            mpremote(["fs", "cp", str(staging / name), ":" + name], port)

        if not args.only_files:
            if not args.server:
                raise SystemExit("--server is required to write config.json")
            if not args.wifi_ssid and not args.factory:
                print("warning: no --wifi-ssid given; the board will not join Wi-Fi")
            config = {
                "wifi_ssid": args.wifi_ssid,
                "wifi_pass": args.wifi_pass,
                "server": args.server.rstrip("/"),
                "token": "" if args.factory else args.token,
                "node_id": args.node or "",
                "heartbeat_s": 10,
                "report_s": 30,
            }
            payload = json.dumps(config)
            print("\n== writing /config.json ==")
            quoted = json.dumps(payload)  # escape for the mpremote exec string
            mpremote(
                ["exec", "open('/config.json','w').write(%s)" % quoted],
                port,
            )

        print("\n== rebooting ==")
        mpremote(["reset"], port, check=False)
        print("\ndone. Watch the board with:  mpremote connect %s repl" % (port or "auto"))
        print("then open the hub's Devices tab - the node should appear within ~15s.")
        return 0
    finally:
        if not args.keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
