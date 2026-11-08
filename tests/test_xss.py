"""Escaping tests: hostile manifest and device names must not become HTML.

The UI builds its controls from the manifests, so a manifest is executable
content as far as the browser is concerned.  These tests feed deliberately
hostile strings through a real server and a real browser and assert that they
render as text.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from ui_test import find_browser
except Exception:  # pragma: no cover
    find_browser = None  # type: ignore[assignment]

from tests.test_e2e import LiveServer  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=1">'


def _browser_available() -> bool:
    if os.environ.get("SMARTHOME_UI_BROWSER") == "0":
        return False
    try:
        find_browser()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="no browser available")


@pytest.fixture()
def hostile_hub(tmp_path: Path):
    """A hub with a device type whose strings are all hostile."""
    data_dir = tmp_path / "xss"
    hidden = tmp_path / "types"
    hidden.mkdir()
    data_dir.mkdir()

    from smarthome import security
    from smarthome.config import Config
    from smarthome.server import create_app

    config_path = data_dir / "config.json"
    config = Config.load(config_path, bootstrap_password=False)
    config.data["auth"]["password_hash"] = security.hash_password("e2e-password")
    config.data["paths"]["device_types_dir"] = str(hidden)
    config.save()

    manifest = {
        "id": "hostile_type",
        "name": HOSTILE,
        "category": "light",
        "icon": "bulb",
        "description": HOSTILE,
        "properties": {
            "power": {"type": "bool", "default": False, "control": "switch", "label": HOSTILE},
            "mode": {
                "type": "enum", "default": "a", "control": "select", "label": HOSTILE,
                "options": [{"value": "a", "label": HOSTILE}, {"value": "b", "label": "b"}],
            },
            "note": {"type": "string", "default": HOSTILE, "control": "text", "label": HOSTILE},
        },
        "config": {"pin": {"type": "int", "min": 0, "max": 48, "default": 4, "label": HOSTILE}},
        "actions": {"boom": {"label": HOSTILE, "args": {}}},
        "events": {},
    }
    (hidden / "hostile.json").write_text(json.dumps(manifest), encoding="utf-8")

    app = create_app(str(config_path))
    import threading
    import time

    import uvicorn

    from tests.test_e2e import free_port

    port = free_port()

    class Server:
        pass

    holder = Server()
    holder.url = "http://127.0.0.1:%d" % port
    holder.port = port
    holder.app = app

    config_obj = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                ws_ping_interval=None, ws_ping_timeout=None)
    server = uvicorn.Server(config_obj)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)

    holder.stop = lambda: (setattr(server, "should_exit", True), thread.join(timeout=10))
    try:
        yield holder
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_hostile_manifest_renders_as_text(hostile_hub):
    import asyncio

    import httpx

    with httpx.Client(base_url=hostile_hub.url, timeout=10) as client:
        client.post("/api/login", json={"password": "e2e-password"})
        created = client.post("/api/devices", json={
            "type": "hostile_type",
            "uid": "hostile-1",
            "name": HOSTILE,
            "room": HOSTILE,
            "config": {"pin": 4},
        })
        assert created.status_code == 201, created.text

    from ui_test import CDP, find_browser, free_port, http_json
    from websockets.asyncio.client import connect
    import subprocess
    import tempfile
    import shutil

    async def check() -> None:
        port = free_port()
        profile = tempfile.mkdtemp(prefix="sh-xss-")
        process = subprocess.Popen(
            [find_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
             "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    http_json("http://127.0.0.1:%d/json/version" % port, timeout=1.0)
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.1)
            page = next(t for t in http_json("http://127.0.0.1:%d/json/list" % port)
                        if t["type"] == "page")
            async with connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as ws:
                cdp = CDP(ws, "")
                await cdp.send("Runtime.enable")
                await cdp.send("Page.enable")
                await cdp.send("Page.navigate", {"url": hostile_hub.url})
                await asyncio.sleep(1.0)
                await cdp.evaluate(
                    "(() => { document.getElementById('password').value = 'e2e-password';"
                    " document.getElementById('login-form')"
                    ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
                )
                await asyncio.sleep(1.5)
                await cdp.send("Page.navigate", {"url": hostile_hub.url})
                await asyncio.sleep(2.5)

                assert await cdp.evaluate("window.__pwned === undefined"), "script executed!"
                injected = await cdp.evaluate("document.querySelectorAll('img').length")
                assert injected == 0, "%d injected <img> elements" % injected
                card_text = await cdp.evaluate(
                    "document.querySelector('.card') ? document.querySelector('.card').textContent : ''"
                )
                assert "<img" in card_text, "the hostile string should be visible as text"
                assert await cdp.evaluate("document.querySelectorAll('.card').length") == 1

                # The open dialogs render the same strings.
                await cdp.evaluate("document.querySelector('.card [data-menu]').click()")
                await asyncio.sleep(0.5)
                assert await cdp.evaluate("window.__pwned === undefined")
                assert await cdp.evaluate("document.querySelectorAll('#dialog img').length") == 0
                await cdp.evaluate("document.getElementById('dialog').close()")

                await cdp.evaluate("document.getElementById('btn-add').click()")
                await asyncio.sleep(0.5)
                assert await cdp.evaluate("window.__pwned === undefined")
                assert await cdp.evaluate("document.querySelectorAll('#dialog img').length") == 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(profile, ignore_errors=True)

    asyncio.run(check())
