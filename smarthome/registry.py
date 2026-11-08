"""Device type registry.

A *device type* is a JSON manifest in ``device_types/``.  It declares the
properties a device exposes (with type, range and UI hint), its actions, the
events it can raise and the hardware ``config`` fields used to bind it to a
microcontroller.  The server and the web UI are completely generic: adding a
new device type means dropping in one file, no server code changes.

Manifest outline::

    {
      "id": "pwm_led_strip",
      "name": "PWM LED Strip",
      "category": "light",
      "icon": "strip",
      "version": 1,
      "properties": {
        "power":      {"type": "bool", "default": false, "control": "switch"},
        "brightness": {"type": "int", "min": 0, "max": 100, "default": 100,
                       "unit": "%", "control": "slider"},
        "color":      {"type": "string", "control": "color", "default": "#ffffff"}
      },
      "config": {
        "pin": {"type": "int", "min": 0, "max": 48, "default": 4, "required": true}
      },
      "actions": {"identify": {"label": "Identify", "args": {}}},
      "events": {"button": {"label": "Button"}}
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import protocol

LOG = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,48}$")
KNOWN_CONTROLS = {
    "switch",
    "slider",
    "number",
    "select",
    "color",
    "text",
    "readonly",
    "color_temp",
    "button",
}
KNOWN_CATEGORIES = {"light", "switch", "sensor", "cover", "climate", "media", "other"}
KNOWN_ICONS = {
    "strip",
    "bulb",
    "plug",
    "relay",
    "thermometer",
    "humidity",
    "motion",
    "button",
    "door",
    "fan",
    "generic",
}


class ManifestError(ValueError):
    """Raised when a device type manifest is malformed."""


@dataclass(frozen=True)
class DeviceType:
    """A validated device type manifest."""

    id: str
    name: str
    version: int
    category: str
    icon: str
    description: str
    properties: Dict[str, Dict[str, Any]]
    config: Dict[str, Dict[str, Any]]
    actions: Dict[str, Dict[str, Any]]
    events: Dict[str, Dict[str, Any]]
    source: str = ""
    order: int = 100

    # -- helpers ---------------------------------------------------------- #
    def default_props(self) -> Dict[str, Any]:
        return protocol.default_props(self)

    def default_config(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, spec in self.config.items():
            if "default" in spec:
                out[key] = spec["default"]
        return out

    def validate_config(self, values: Mapping[str, Any], *, strict: bool = True) -> Dict[str, Any]:
        """Validate hardware config against the manifest schema.

        ``strict`` (the default, used when a human edits the config) rejects
        unknown keys and out-of-range values.  With ``strict=False`` — used for
        config *reported by a node* — a bad or unknown field falls back to its
        default instead of failing the whole document, so firmware and hub
        versions can differ without losing every setting.
        """
        clean: Dict[str, Any] = {}
        errors: List[str] = []
        for key, spec in self.config.items():
            if key in (values or {}):
                try:
                    clean[key] = protocol.coerce_value(spec, values[key])
                    continue
                except (ValueError, TypeError) as exc:
                    if strict:
                        errors.append("%s: %s" % (key, exc))
                    else:
                        LOG.debug("%s: %s: %s, using the default", self.id, key, exc)
            if "default" in spec:
                clean[key] = spec["default"]
            elif spec.get("required"):
                errors.append("%s is required" % key)
        for key in (values or {}):
            if key not in self.config:
                if strict:
                    errors.append("unknown config field %r" % (key,))
                else:
                    LOG.debug("%s: ignoring unknown config field %r", self.id, key)
        if errors and strict:
            raise ManifestError("; ".join(errors))
        return clean

    def validate_args(self, action: str, args: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        spec = self.actions.get(action)
        if spec is None:
            raise ManifestError("unknown action %r" % (action,))
        schema: Dict[str, Dict[str, Any]] = spec.get("args") or {}
        clean: Dict[str, Any] = {}
        errors: List[str] = []
        for key, arg_spec in schema.items():
            if args is not None and key in args:
                try:
                    clean[key] = protocol.coerce_value(arg_spec, args[key])
                except (ValueError, TypeError) as exc:
                    errors.append("%s: %s" % (key, exc))
            elif "default" in arg_spec:
                clean[key] = arg_spec["default"]
            elif arg_spec.get("required"):
                errors.append("%s is required" % key)
        if errors:
            raise ManifestError("; ".join(errors))
        return clean

    # -- serialisation ---------------------------------------------------- #
    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "icon": self.icon,
            "description": self.description,
            "properties": self.properties,
            "config": self.config,
            "actions": self.actions,
            "events": self.events,
            "source": self.source,
        }


def _validate_property(name: str, spec: Any) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise ManifestError("property %r must be an object" % (name,))
    kind = spec.get("type", "string")
    if kind not in ("bool", "int", "float", "string", "enum"):
        raise ManifestError("property %r has unsupported type %r" % (name, kind))
    clean = dict(spec)
    control = clean.get("control")
    if control is None:
        control = {
            "bool": "switch",
            "int": "slider",
            "float": "slider",
            "string": "text",
            "enum": "select",
        }[kind]
    if control not in KNOWN_CONTROLS:
        raise ManifestError("property %r has unsupported control %r" % (name, control))
    clean["control"] = control
    if kind in ("int", "float"):
        low, high = clean.get("min"), clean.get("max")
        if low is not None and high is not None and float(low) > float(high):
            raise ManifestError("property %r has min > max" % (name,))
        default = clean.get("default")
        if default is not None:
            try:
                protocol.coerce_value(clean, default)
            except (ValueError, TypeError) as exc:
                raise ManifestError("property %r default invalid: %s" % (name, exc))
    elif kind == "enum":
        options = clean.get("options")
        if not isinstance(options, list) or not options:
            raise ManifestError("enum property %r needs a non-empty options list" % (name,))
        values = [opt.get("value") if isinstance(opt, dict) else opt for opt in options]
        if "default" not in clean:
            clean["default"] = values[0]
        if clean["default"] not in values:
            raise ManifestError("property %r default is not one of its options" % (name,))
    elif kind == "bool":
        clean.setdefault("default", False)
    elif kind == "string":
        if clean.get("control") == "color":
            default = clean.get("default", "#ffffff")
            if not protocol._HEX_COLOR.match(str(default)):
                raise ManifestError("property %r colour default must look like #RRGGBB" % (name,))
            clean["default"] = default
    return clean


def _normalise(manifest: Mapping[str, Any], source: str) -> DeviceType:
    type_id = str(manifest.get("id") or "").strip()
    if not ID_RE.match(type_id):
        raise ManifestError("invalid or missing type id %r (lowercase letters, digits, _)" % (type_id,))
    name = str(manifest.get("name") or type_id)
    try:
        version = int(manifest.get("version", 1))
    except (TypeError, ValueError):
        raise ManifestError("version must be an integer")
    category = str(manifest.get("category") or "other")
    if category not in KNOWN_CATEGORIES:
        raise ManifestError("unknown category %r" % (category,))
    icon = str(manifest.get("icon") or "generic")
    if icon not in KNOWN_ICONS:
        raise ManifestError("unknown icon %r" % (icon,))

    raw_props = manifest.get("properties") or {}
    if not isinstance(raw_props, dict):
        raise ManifestError("properties must be an object")
    props = {key: _validate_property(key, spec) for key, spec in raw_props.items()}

    raw_config = manifest.get("config") or {}
    if not isinstance(raw_config, dict):
        raise ManifestError("config must be an object")
    config = {key: _validate_property(key, spec) for key, spec in raw_config.items()}

    raw_actions = manifest.get("actions") or {}
    if not isinstance(raw_actions, dict):
        raise ManifestError("actions must be an object")
    actions: Dict[str, Dict[str, Any]] = {}
    for action, spec in raw_actions.items():
        if not isinstance(spec, dict):
            raise ManifestError("action %r must be an object" % (action,))
        args_schema = spec.get("args") or {}
        if not isinstance(args_schema, dict):
            raise ManifestError("action %r args must be an object" % (action,))
        actions[action] = {
            "label": str(spec.get("label") or action.replace("_", " ").title()),
            "description": str(spec.get("description") or ""),
            "args": {key: _validate_property(key, arg) for key, arg in args_schema.items()},
        }

    raw_events = manifest.get("events") or {}
    if not isinstance(raw_events, dict):
        raise ManifestError("events must be an object")
    events = {
        str(key): {"label": str((spec or {}).get("label") or key) if isinstance(spec, dict) else str(key)}
        for key, spec in raw_events.items()
    }

    try:
        order = int(manifest.get("order", 100))
    except (TypeError, ValueError):
        order = 100

    return DeviceType(
        id=type_id,
        name=name,
        version=version,
        category=category,
        icon=icon,
        description=str(manifest.get("description") or ""),
        properties=props,
        config=config,
        actions=actions,
        events=events,
        source=source,
        order=order,
    )


class TypeRegistry:
    """Loads and serves device type manifests from one or more directories."""

    def __init__(self, directories: Iterable[Path]):
        self.directories = [Path(d) for d in directories]
        self._types: Dict[str, DeviceType] = {}
        self.errors: Dict[str, str] = {}

    # -- loading ---------------------------------------------------------- #
    def reload(self) -> Dict[str, str]:
        types: Dict[str, DeviceType] = {}
        errors: Dict[str, str] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        raise ManifestError("manifest must be a JSON object")
                    dtype = _normalise(manifest, str(path))
                except (OSError, ValueError) as exc:
                    errors[path.name] = str(exc)
                    LOG.warning("ignoring device type %s: %s", path, exc)
                    continue
                existing = types.get(dtype.id)
                if existing is not None:
                    errors[path.name] = "duplicate type id %r (already from %s)" % (dtype.id, existing.source)
                    LOG.warning("device type %s ignored: %s", path, errors[path.name])
                    continue
                types[dtype.id] = dtype
        self._types = types
        self.errors = errors
        LOG.info("loaded %d device types from %s", len(types), ", ".join(str(d) for d in self.directories))
        return errors

    def ensure_loaded(self) -> None:
        if not self._types and not self.errors:
            self.reload()

    # -- access ----------------------------------------------------------- #
    def get(self, type_id: str) -> Optional[DeviceType]:
        self.ensure_loaded()
        return self._types.get(type_id)

    def require(self, type_id: str) -> DeviceType:
        dtype = self.get(type_id)
        if dtype is None:
            raise KeyError(type_id)
        return dtype

    def all(self) -> List[DeviceType]:
        self.ensure_loaded()
        return sorted(self._types.values(), key=lambda t: (t.order, t.name))

    def ids(self) -> List[str]:
        return [t.id for t in self.all()]

    def public(self) -> List[Dict[str, Any]]:
        return [t.public() for t in self.all()]
