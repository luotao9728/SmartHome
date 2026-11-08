"""Configuration handling for the SmartHome hub.

The configuration is a small JSON document stored next to the database.  It is
created automatically on first start (together with a random session secret and
an admin password that is printed once to the console).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from . import security

CONFIG_VERSION = 1

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "behind_proxy": False,
    },
    "auth": {
        "password_hash": "",
        "session_ttl_hours": 24 * 14,
        "cookie_secure": False,
        "api_key": "",
        "require_node_tokens": True,
        "login_max_failures": 5,
        "login_lockout_s": 60,
    },
    "devices": {
        # How often the hub pings every device (and how often the device reports
        # its state proactively).  Both are in seconds.
        "heartbeat_s": 10,
        "report_s": 30,
        # A device that has been silent for this long is marked offline.  Must
        # be comfortably larger than heartbeat_s * 2.
        "offline_after_s": 40,
        # Command acknowledgement: wait this long, then retry, then give up.
        "ack_timeout_s": 3.0,
        "ack_retries": 2,
        # Keep this many days of state history.
        "history_days": 30,
    },
    "ui": {
        "title": "SmartHome",
        "theme": "auto",
    },
    "paths": {
        # Extra directory that may contain user supplied device types.  Relative
        # paths are resolved against the data directory.
        "device_types_dir": "",
    },
}


def default_data_dir() -> Path:
    env = os.environ.get("SMARTHOME_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".smarthome"


#: Where a system-wide (packaged / systemd) install keeps its data.  Checked so
#: that ``set-password`` and friends edit the file the *service* reads even when
#: they are run without the unit's environment (e.g. `sudo -u smarthome`).
SYSTEM_DATA_DIR = Path("/var/lib/smarthome")


def discover_config_path() -> Path:
    """Locate the configuration the running hub actually uses.

    Resolution order:

    1. ``$SMARTHOME_CONFIG`` - explicit, always wins.
    2. ``<SMARTHOME_DATA_DIR>/config.json`` when that variable is set.
    3. ``/var/lib/smarthome/config.json`` when it exists (a systemd install).
    4. ``~/.smarthome/config.json`` - the per-user default.

    Without step 3 a maintenance command run by hand would quietly write to the
    invoking user's home directory while the service kept reading its own file,
    which makes ``set-password`` appear to do nothing.
    """
    explicit = os.environ.get("SMARTHOME_CONFIG")
    if explicit:
        return Path(explicit).expanduser()

    data_env = os.environ.get("SMARTHOME_DATA_DIR")
    if data_env:
        return Path(data_env).expanduser() / "config.json"

    system_config = SYSTEM_DATA_DIR / "config.json"
    try:
        if system_config.is_file():
            return system_config
    except OSError:
        pass

    return Path.home() / ".smarthome" / "config.json"


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class Config:
    """Typed access to the JSON configuration."""

    def __init__(self, path: Path, data: Dict[str, Any], data_dir: Path, bundled_types_dir: Path):
        self.path = path
        self.data = data
        self.data_dir = data_dir
        self.bundled_types_dir = bundled_types_dir
        self._generated_password: Optional[str] = None

    # -- construction ----------------------------------------------------- #
    @classmethod
    def load(
        cls,
        path: Optional[os.PathLike[str] | str] = None,
        *,
        bundled_types_dir: Optional[Path] = None,
        create: bool = True,
        bootstrap_password: bool = True,
    ) -> "Config":
        config_path = Path(path).expanduser() if path else discover_config_path()
        data_dir = config_path.parent
        if bundled_types_dir is None:
            bundled_types_dir = Path(__file__).resolve().parent.parent / "device_types"

        raw: Dict[str, Any] = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("cannot read config file %s: %s" % (config_path, exc))
            if not isinstance(raw, dict):
                raise RuntimeError("config file %s must contain a JSON object" % config_path)

        data = _deep_merge(DEFAULT_CONFIG, raw)
        cfg = cls(config_path, data, data_dir, bundled_types_dir)
        cfg._apply_env_overrides()

        changed = not config_path.exists()
        if not data["auth"].get("password_hash") and bootstrap_password:
            password = security.generate_password()
            data["auth"]["password_hash"] = security.hash_password(password)
            cfg._generated_password = password
            changed = True
        if changed or not config_path.exists():
            data["version"] = CONFIG_VERSION
            cfg.save()
        return cfg

    def _apply_env_overrides(self) -> None:
        if os.environ.get("SMARTHOME_HOST"):
            self.data["server"]["host"] = os.environ["SMARTHOME_HOST"]
        if os.environ.get("SMARTHOME_PORT"):
            self.data["server"]["port"] = int(os.environ["SMARTHOME_PORT"])

    # -- persistence ------------------------------------------------------ #
    def save(self) -> None:
        text = json.dumps(self.data, indent=2, sort_keys=False) + "\n"
        _atomic_write(self.path, text)

    # -- convenience ------------------------------------------------------ #
    @property
    def server(self) -> Dict[str, Any]:
        return self.data["server"]

    @property
    def auth(self) -> Dict[str, Any]:
        return self.data["auth"]

    @property
    def devices(self) -> Dict[str, Any]:
        return self.data["devices"]

    @property
    def ui(self) -> Dict[str, Any]:
        return self.data["ui"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "smarthome.db"

    @property
    def device_types_dirs(self) -> list[Path]:
        dirs = [self.bundled_types_dir]
        extra = (self.data.get("paths") or {}).get("device_types_dir")
        if extra:
            extra_path = Path(extra).expanduser()
            if not extra_path.is_absolute():
                extra_path = self.data_dir / extra_path
            if extra_path != self.bundled_types_dir:
                dirs.append(extra_path)
        return dirs

    @property
    def generated_password(self) -> Optional[str]:
        return self._generated_password

    def set_admin_password(self, password: str) -> None:
        self.data["auth"]["password_hash"] = security.hash_password(password)
        self.save()

    # -- session ---------------------------------------------------------- #
    def _stored_value(self, key: str) -> str:
        """Read a value another process may have written to the config file."""
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        value = ((stored or {}).get("auth") or {}).get(key)
        return value if isinstance(value, str) else ""

    def session_secret(self) -> str:
        """Session signing secret, generated on demand and persisted."""
        secret = self.data["auth"].get("session_secret") or self._stored_value("session_secret")
        if not secret:
            secret = security.new_secret()
        if self.data["auth"].get("session_secret") != secret:
            self.data["auth"]["session_secret"] = secret
            self.save()
        return secret

    def api_key(self) -> str:
        key = self.data["auth"].get("api_key") or self._stored_value("api_key")
        if not key:
            key = "shk_" + security.new_secret()
        if self.data["auth"].get("api_key") != key:
            self.data["auth"]["api_key"] = key
            self.save()
        return key
