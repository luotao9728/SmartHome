"""Restartable dev server + a simulated node, for screenshots."""

import asyncio, os, sys, tempfile, pathlib
sys.path.insert(0, '.')

import uvicorn

from smarthome import security
from smarthome.config import Config
from smarthome.server import create_app

data = pathlib.Path(os.environ.get("SMARTHOME_DEV_DATA") or tempfile.mkdtemp(prefix="sh-dev-"))
data.mkdir(parents=True, exist_ok=True)
cfg_path = data / "config.json"
os.environ["SMARTHOME_DATA_DIR"] = str(data)

cfg = Config.load(cfg_path, bootstrap_password=False)
if not cfg.data["auth"].get("password_hash") or os.environ.get("SMARTHOME_DEV_RESET"):
    cfg.data["auth"]["password_hash"] = security.hash_password("test-password")
if os.environ.get("SMARTHOME_DEV_APIKEY"):
    cfg.data["auth"]["api_key"] = os.environ["SMARTHOME_DEV_APIKEY"]
cfg.data["devices"].update({"heartbeat_s": 5, "offline_after_s": 15})
cfg.data["ui"]["title"] = "SmartHome"
cfg.save()

app = create_app(str(cfg_path))
print("DEV DATA:", data, flush=True)
print("DEV KEY:", app.state.smart.config.api_key(), flush=True)
uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SMARTHOME_DEV_PORT", "8099")),
            log_level="warning", ws_ping_interval=None, ws_ping_timeout=None)
