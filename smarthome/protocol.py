"""Node protocol: message builders and property validation.

Everything that crosses the wire between the Pi and a microcontroller is a
single JSON object in one WebSocket text frame::

    {"t": "cmd", "id": "c12", "dev": "strip-desk", "props": {"brightness": 40}}

The same schema is used for the desired/reported state stored by the hub, so a
device type is fully described by its manifest (``device_types/*.json``).

Keep this module dependency free: it is imported by the server only, but the
rules here are mirrored (in a much smaller form) by the MicroPython client.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

PROTOCOL_VERSION = 1

# Message types ------------------------------------------------------------ #
# hub -> node
HELLO = "hello"            # server greets a freshly connected node
CMD = "cmd"                # apply property values
ACT = "act"                # run a device action
STATE_REQ = "state_req"    # ask the node to report every device
PING = "ping"
PONG = "pong"
CONFIG = "config"          # timing changed, adopt new values
REBOOT = "reboot"
BYE = "bye"                # server is going away (restart), reconnect soon

# node -> hub
NODE_HELLO = "hello"
STATE = "state"
ACK = "ack"
EVENT = "event"
LOG = "log"

FRAME_MAX = 64 * 1024

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ProtocolError(ValueError):
    """Raised when a node sends something the hub cannot accept."""


# --------------------------------------------------------------------------- #
# Property coercion / validation
# --------------------------------------------------------------------------- #
def _props_of(manifest: Any) -> Mapping[str, Any]:
    """Accept either a raw manifest mapping or a ``registry.DeviceType``."""
    if hasattr(manifest, "properties"):
        return manifest.properties  # type: ignore[no-any-return]
    return (manifest or {}).get("properties") or {}


def default_props(manifest: Any) -> Dict[str, Any]:
    """Defaults for the *settable* properties only.

    Read-only properties (sensor readings) have defaults too, but putting them
    in ``desired`` would mean the hub claims the user asked for 0 °C — and the
    UI would show that instead of the real reading.
    """
    props: Dict[str, Any] = {}
    for key, spec in _props_of(manifest).items():
        if spec.get("settable", True) and "default" in spec:
            props[key] = spec["default"]
    return props


def settable_props(manifest: Any) -> Dict[str, Any]:
    return {
        key: spec
        for key, spec in _props_of(manifest).items()
        if spec.get("settable", True)
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "on", "yes"):
            return True
        if lowered in ("0", "false", "off", "no"):
            return False
    raise ValueError("expected a boolean")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ValueError("expected a whole number")
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            pass
    raise ValueError("expected a whole number")


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("expected a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise ValueError("expected a number")
    else:
        raise ValueError("expected a number")
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number


def coerce_value(spec: Mapping[str, Any], value: Any) -> Any:
    """Convert ``value`` for a manifest property, or raise ``ValueError``."""
    kind = spec.get("type", "string")
    if kind == "bool":
        return _as_bool(value)
    if kind == "int":
        number = _as_int(value)
    elif kind == "float":
        number = _as_float(value)
    elif kind == "enum":
        options = [opt.get("value") if isinstance(opt, dict) else opt for opt in spec.get("options", [])]
        if value not in options:
            raise ValueError("expected one of %s" % ", ".join(repr(o) for o in options))
        return value
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("expected a string")
        max_len = int(spec.get("max_len") or 255)
        if len(value) > max_len:
            raise ValueError("expected at most %d characters" % max_len)
        if spec.get("control") == "color" and not _HEX_COLOR.match(value):
            raise ValueError("expected a colour like #RRGGBB")
        return value
    else:
        raise ValueError("unknown property type %r" % (kind,))

    low, high = spec.get("min"), spec.get("max")
    if low is not None and number < low:
        raise ValueError("must be >= %s" % low)
    if high is not None and number > high:
        raise ValueError("must be <= %s" % high)
    return number


def normalize_props(
    manifest: Any,
    values: Mapping[str, Any],
    *,
    only_settable: bool = True,
    ignore_unknown: bool = False,
) -> Tuple[Dict[str, Any], list[str]]:
    """Validate a property update.

    Returns ``(clean, errors)``.  When ``ignore_unknown`` is set (used for
    state reported *by* a node) unknown keys are dropped silently, which keeps
    an older hub talking to a newer firmware.
    """
    specs = _props_of(manifest)
    clean: Dict[str, Any] = {}
    errors: list[str] = []
    for key, value in (values or {}).items():
        spec = specs.get(key)
        if spec is None:
            if not ignore_unknown:
                errors.append("unknown property %r" % (key,))
            continue
        if only_settable and not spec.get("settable", True):
            if not ignore_unknown:
                errors.append("property %r is read only" % (key,))
            continue
        try:
            clean[key] = coerce_value(spec, value)
        except (ValueError, TypeError) as exc:
            errors.append("%s: %s" % (key, exc))
    return clean, errors


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) and type(a) is type(b) or (isinstance(a, bool) and isinstance(b, bool) and a == b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    return a == b


def diff_desired(manifest: Any, desired: Mapping[str, Any], reported: Mapping[str, Any]) -> Dict[str, Any]:
    """Set of desired values that differ from what the device reports.

    Only properties the node actually reported are considered: a node reports
    every property it implements, so a key that never appears belongs to a
    firmware that does not know about it and pushing it would only produce
    errors.
    """
    specs = _props_of(manifest)
    pending: Dict[str, Any] = {}
    for key, value in desired.items():
        spec = specs.get(key)
        if spec is None or not spec.get("settable", True):
            continue
        if key not in reported:
            continue
        if not values_equal(value, reported[key]):
            pending[key] = value
    return pending


# --------------------------------------------------------------------------- #
# Message builders (hub side)
# --------------------------------------------------------------------------- #
def hello(cfg: Mapping[str, Any], *, node_id: str, server_time: float) -> Dict[str, Any]:
    return {
        "t": HELLO,
        "v": PROTOCOL_VERSION,
        "node": node_id,
        "time": round(server_time, 3),
        "heartbeat_s": cfg["heartbeat_s"],
        "report_s": cfg["report_s"],
        "ack_timeout_s": cfg["ack_timeout_s"],
    }


def cmd(msg_id: str, uid: str, props: Mapping[str, Any], *, ts: Optional[float] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"t": CMD, "id": msg_id, "dev": uid, "props": dict(props)}
    if ts is not None:
        message["ts"] = round(ts, 3)
    return message


def act(msg_id: str, uid: str, action: str, args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {"t": ACT, "id": msg_id, "dev": uid, "action": action, "args": dict(args or {})}


def state_req(msg_id: str) -> Dict[str, Any]:
    return {"t": STATE_REQ, "id": msg_id}


def ping(seq: int, ts: float) -> Dict[str, Any]:
    return {"t": PING, "n": seq, "ts": round(ts, 3)}


def pong(seq: int) -> Dict[str, Any]:
    return {"t": PONG, "n": seq}


def config_message(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "t": CONFIG,
        "heartbeat_s": cfg["heartbeat_s"],
        "report_s": cfg["report_s"],
        "ack_timeout_s": cfg["ack_timeout_s"],
    }


# --------------------------------------------------------------------------- #
# Message parsing (node side)
# --------------------------------------------------------------------------- #
def parse_node_message(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProtocolError("message must be a JSON object")
    kind = raw.get("t")
    if not isinstance(kind, str):
        raise ProtocolError("message is missing a type")
    return raw


def _require(raw: Mapping[str, Any], key: str, kind: type) -> Any:
    value = raw.get(key)
    if not isinstance(value, kind):
        raise ProtocolError("field %r must be a %s" % (key, kind.__name__))
    return value


def parse_hello(raw: Mapping[str, Any]) -> Dict[str, Any]:
    devices = raw.get("devices")
    if not isinstance(devices, list):
        raise ProtocolError("hello.devices must be a list")
    parsed = []
    for entry in devices:
        if not isinstance(entry, dict):
            raise ProtocolError("hello.devices entries must be objects")
        uid = entry.get("uid")
        dev_type = entry.get("type")
        if not isinstance(uid, str) or not uid:
            raise ProtocolError("device uid missing")
        if not isinstance(dev_type, str) or not dev_type:
            raise ProtocolError("device type missing for %r" % (uid,))
        state = entry.get("state") or {}
        if not isinstance(state, dict):
            raise ProtocolError("device state must be an object")
        config = entry.get("config") or {}
        if not isinstance(config, dict):
            raise ProtocolError("device config must be an object for %r" % (uid,))
        parsed.append(
            {
                "uid": uid,
                "type": dev_type,
                "name": entry.get("name") if isinstance(entry.get("name"), str) else uid,
                "state": state,
                # Hardware configuration travels with the announcement so the
                # hub records what the board is actually wired to (pin numbers,
                # gamma, ...) instead of guessing from manifest defaults.
                "config": config,
            }
        )
    if not parsed:
        raise ProtocolError("node reported no devices")
    return {
        "node_id": raw.get("node") if isinstance(raw.get("node"), str) else None,
        "fw": raw.get("fw") if isinstance(raw.get("fw"), str) else "",
        "board": raw.get("board") if isinstance(raw.get("board"), str) else "",
        "ip": raw.get("ip") if isinstance(raw.get("ip"), str) else "",
        "rssi": raw.get("rssi") if isinstance(raw.get("rssi"), (int, float)) else None,
        "uptime": raw.get("uptime") if isinstance(raw.get("uptime"), (int, float)) else None,
        "devices": parsed,
    }


def parse_state(raw: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    uid = _require(raw, "dev", str)
    values = raw.get("values")
    if not isinstance(values, dict):
        raise ProtocolError("state.values must be an object")
    return uid, values


def parse_ack(raw: Mapping[str, Any]) -> Dict[str, Any]:
    msg_id = _require(raw, "id", str)
    ok = raw.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError("ack.ok must be a boolean")
    state = raw.get("state")
    if state is not None and not isinstance(state, dict):
        raise ProtocolError("ack.state must be an object")
    return {
        "id": msg_id,
        "ok": ok,
        "error": raw.get("error") if isinstance(raw.get("error"), str) else "",
        "state": state or {},
    }


def parse_event(raw: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "dev": raw.get("dev") if isinstance(raw.get("dev"), str) else "",
        "event": raw.get("event") if isinstance(raw.get("event"), str) else "event",
        "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
    }


def chunked(items: Iterable[Any], size: int) -> list[list[Any]]:
    batch: list[Any] = []
    out: list[list[Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            out.append(batch)
            batch = []
    if batch:
        out.append(batch)
    return out
