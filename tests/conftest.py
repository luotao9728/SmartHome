"""Test fixtures."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smarthome import security  # noqa: E402
from smarthome.config import Config  # noqa: E402
from smarthome.hub import Hub  # noqa: E402
from smarthome.registry import TypeRegistry  # noqa: E402
from smarthome.store import Store  # noqa: E402

PASSWORD = "correct horse battery"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture()
def config(data_dir: Path) -> Config:
    cfg = Config.load(data_dir / "config.json", bootstrap_password=False)
    cfg.data["auth"]["password_hash"] = security.hash_password(PASSWORD)
    cfg.data["devices"]["ack_timeout_s"] = 0.3
    cfg.data["devices"]["heartbeat_s"] = 0.4
    cfg.data["devices"]["offline_after_s"] = 1.5
    cfg.save()
    return cfg


@pytest.fixture()
def store(config: Config) -> Store:
    store = Store(config.db_path)
    yield store
    store.close()


@pytest.fixture()
def registry(config: Config) -> TypeRegistry:
    reg = TypeRegistry(config.device_types_dirs)
    reg.reload()
    return reg


@pytest.fixture()
def hub(config: Config, store: Store, registry: TypeRegistry) -> Hub:
    hub = Hub(config, store, registry)
    yield hub
    store.close()


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"
