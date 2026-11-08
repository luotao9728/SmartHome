"""Command line entry point.

    python -m smarthome serve [--host 0.0.0.0] [--port 8080]
    python -m smarthome set-password [--password ...]
    python -m smarthome apikey [--rotate]
    python -m smarthome node-token NODE_ID
    python -m smarthome types [--reload]
    python -m smarthome devices
    python -m smarthome backup [PATH]
    python -m smarthome info

All commands accept ``--config PATH`` (default ``$SMARTHOME_CONFIG`` or
``~/.smarthome/config.json``).
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__, security
from .config import Config, default_data_dir
from .nodes import NodeAuth
from .registry import TypeRegistry
from .store import Store

LOG = logging.getLogger("smarthome.cli")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=os.environ.get("SMARTHOME_CONFIG"),
                        help="path to config.json")


def _config(args: argparse.Namespace, *, bootstrap_password: bool = False) -> Config:
    return Config.load(args.config, bootstrap_password=bootstrap_password)


def _lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        info = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        addresses.update(item[4][0] for item in info)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        addresses.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .logging_setup import setup_logging
    from .server import create_app

    config = _config(args, bootstrap_password=True)
    overrides = False
    if args.host:
        config.data["server"]["host"] = args.host
        overrides = True
    if args.port:
        config.data["server"]["port"] = int(args.port)
        overrides = True
    if overrides:
        # ``create_app`` reloads the file, so persist the overrides first: the
        # banner and the API must report the address actually in use.
        config.save()
    setup_logging(level=args.log_level, path=config.data_dir / "smarthome.log" if args.log_file else None)

    if config.generated_password:
        print("\nAdmin password generated: %s\n" % config.generated_password, flush=True)

    app = create_app(str(config.path))
    uvicorn.run(
        app,
        host=config.data["server"]["host"],
        port=int(config.data["server"]["port"]),
        log_level=args.log_level.lower(),
        access_log=bool(args.access_log),
        ws_ping_interval=None,   # the hub implements its own heartbeats
        ws_ping_timeout=None,
        timeout_keep_alive=30,
        proxy_headers=bool(config.server.get("behind_proxy")),
    )
    return 0


def _running_service_config() -> Optional[Path]:
    """Config file the smart*home systemd unit uses, if we can tell."""
    try:
        completed = subprocess.run(
            ["systemctl", "show", "smarthome.service", "--property=Environment"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for token in completed.stdout.split():
        if token.startswith("SMARTHOME_CONFIG="):
            value = token.split("=", 1)[1].strip().strip('"')
            return Path(value) if value else None
    return None


def cmd_set_password(args: argparse.Namespace) -> int:
    config = _config(args)
    password = args.password
    if not password:
        first = getpass.getpass("New password: ")
        second = getpass.getpass("Repeat password: ")
        if first != second:
            print("passwords do not match", file=sys.stderr)
            return 1
        password = first

    # Guard against the classic footgun: writing the hash to a config the
    # service never reads (it keeps the old password, so nothing seems to
    # happen).  Only a warning -- there are legitimate reasons for a split.
    service_config = _running_service_config()
    if service_config is not None and service_config != config.path:
        print("warning: the running service uses %s" % service_config, file=sys.stderr)
        print("         but this command is about to edit %s" % config.path, file=sys.stderr)
        print("         Re-run with --config %s to change the service's password."
              % service_config, file=sys.stderr)

    try:
        config.set_admin_password(password)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: cannot write %s: %s" % (config.path, exc), file=sys.stderr)
        if os.geteuid() != 0 if hasattr(os, "geteuid") else False:
            print("hint: try again with sudo", file=sys.stderr)
        return 1

    print("password updated in %s" % config.path)
    if service_config is not None and service_config == config.path:
        print("the running service reads this file; no restart needed")
    return 0


def cmd_apikey(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.rotate:
        config.data["auth"]["api_key"] = "shk_" + security.new_secret()
        config.save()
        print("API key rotated:")
    else:
        print("API key:")
    print(config.api_key())
    return 0


def cmd_node_token(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    try:
        auth = NodeAuth(store, require_tokens=True)
        token = auth.issue_token(args.node_id)
        print("Token for node %s (copy into the board's config.json):" % args.node_id)
        print(token)
        print("\nOr run, on the board:")
        print("  mpremote connect /dev/ttyACM0 exec "
              "\"import json; json.dump({'wifi_ssid':'..','wifi_pass':'..','server':'http://%s:%d',"
              "'node_id':'%s','token':'%s'}, open('/config.json','w'))\""
              % ("<pi-ip>", config.server["port"], args.node_id, token))
        return 0
    finally:
        store.close()


def cmd_types(args: argparse.Namespace) -> int:
    config = _config(args)
    registry = TypeRegistry(config.device_types_dirs)
    errors = registry.reload()
    print("%d device types:" % len(registry.ids()))
    for dtype in registry.all():
        print("  %-22s %-28s v%d  %s" % (dtype.id, dtype.name, dtype.version, dtype.description[:48]))
    for name, message in errors.items():
        print("  ! %s: %s" % (name, message), file=sys.stderr)
    dirs = ", ".join(str(d) for d in config.device_types_dirs)
    print("\nsearch path: %s" % dirs)
    return 1 if errors else 0


def cmd_devices(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    try:
        devices = store.list_devices()
        if not devices:
            print("no devices yet")
            return 0
        print("%-28s %-20s %-16s %s" % ("UID", "NAME", "TYPE", "NODE"))
        for device in devices:
            print("%-28s %-20s %-16s %s" % (
                device["uid"], device["name"][:20], device["type"],
                device.get("node_id") or "-",
            ))
        nodes = store.list_nodes()
        print("\nknown nodes: %s" % (", ".join(n["node_id"] for n in nodes) or "none"))
        return 0
    finally:
        store.close()


def cmd_backup(args: argparse.Namespace) -> int:
    config = _config(args)
    target = Path(args.path) if args.path else default_data_dir() / (
        "backup-%s" % time.strftime("%Y%m%d-%H%M%S")
    )
    target.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "smarthome.db", "smarthome.db-wal", "smarthome.db-shm"):
        source = config.data_dir / name
        if source.exists():
            shutil.copy2(source, target / name)
    print("backup written to %s" % target)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    try:
        devices = store.list_devices()
        nodes = store.list_nodes()
        print("SmartHome %s" % __version__)
        print("config      : %s" % config.path)
        print("database    : %s" % config.db_path)
        print("device types: %s" % ", ".join(str(d) for d in config.device_types_dirs))
        print("devices     : %d" % len(devices))
        print("nodes       : %d (%s)" % (len(nodes), ", ".join(n["node_id"] for n in nodes) or "none"))
        print("port        : %s" % config.server["port"])
        addresses = _lan_addresses()
        if addresses:
            print("URLs        : %s" % ", ".join("http://%s:%s" % (a, config.server["port"]) for a in addresses))
        return 0
    finally:
        store.close()


def cmd_export_types(args: argparse.Namespace) -> int:
    config = _config(args)
    registry = TypeRegistry(config.device_types_dirs)
    registry.reload()
    print(json.dumps(registry.public(), indent=2))
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smarthome", description="SmartHome hub for Raspberry Pi")
    parser.add_argument("--version", action="version", version="SmartHome %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web server")
    _add_common(serve)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info",
                       choices=["debug", "info", "warning", "error", "critical"])
    serve.add_argument("--log-file", action="store_true", help="also log to the data directory")
    serve.add_argument("--access-log", action="store_true", help="log every HTTP request")
    serve.set_defaults(func=cmd_serve)

    password = sub.add_parser("set-password", help="change the admin password")
    _add_common(password)
    password.add_argument("--password")
    password.set_defaults(func=cmd_set_password)

    apikey = sub.add_parser("apikey", help="show or rotate the API key")
    _add_common(apikey)
    apikey.add_argument("--rotate", action="store_true")
    apikey.set_defaults(func=cmd_apikey)

    token = sub.add_parser("node-token", help="issue a device token for a node")
    _add_common(token)
    token.add_argument("node_id")
    token.set_defaults(func=cmd_node_token)

    types = sub.add_parser("types", help="list device types and manifest errors")
    _add_common(types)
    types.set_defaults(func=cmd_types)

    devices = sub.add_parser("devices", help="list devices and nodes")
    _add_common(devices)
    devices.set_defaults(func=cmd_devices)

    backup = sub.add_parser("backup", help="copy config and database")
    _add_common(backup)
    backup.add_argument("path", nargs="?")
    backup.set_defaults(func=cmd_backup)

    info = sub.add_parser("info", help="show paths and status")
    _add_common(info)
    info.set_defaults(func=cmd_info)

    export = sub.add_parser("export-types", help="print device type manifests as JSON")
    _add_common(export)
    export.set_defaults(func=cmd_export_types)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("SMARTHOME_DEBUG"):
            raise
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
