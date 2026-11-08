"""Browser tests for the multi-user UI: login, Users tab, setup page."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from ui_test import find_browser
except Exception:  # pragma: no cover
    find_browser = None  # type: ignore[assignment]

from tests.test_e2e import PASSWORD, LiveServer  # noqa: E402


def _browser_available() -> bool:
    if os.environ.get("SMARTHOME_UI_BROWSER") == "0":
        return False
    try:
        find_browser()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="no browser available")


class Browser:
    """Small helper around a headless browser tab."""

    def __init__(self, url: str):
        self.url = url

    async def __aenter__(self):
        from ui_test import CDP, find_browser, free_port, http_json
        from websockets.asyncio.client import connect

        self._port = free_port()
        self._profile = tempfile.mkdtemp(prefix="sh-users-")
        self._process = subprocess.Popen(
            [find_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
             "--remote-debugging-port=%d" % self._port,
             "--user-data-dir=" + self._profile, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                http_json("http://127.0.0.1:%d/json/version" % self._port, timeout=1.0)
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.1)
        page = next(t for t in http_json("http://127.0.0.1:%d/json/list" % self._port)
                    if t["type"] == "page")
        self._ws = await connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024)
        self.cdp = CDP(self._ws, "")
        await self.cdp.send("Runtime.enable")
        await self.cdp.send("Page.enable")
        return self

    async def __aexit__(self, *exc):
        try:
            await self._ws.close()
        finally:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            shutil.rmtree(self._profile, ignore_errors=True)

    async def goto(self, path: str = "/"):
        await self.cdp.send("Page.navigate", {"url": self.url.rstrip("/") + path})
        await asyncio.sleep(1.2)

    async def login(self, password: str, username: str = ""):
        await self.goto("/")
        await self.cdp.wait_for("!!document.getElementById('login-form')")
        await self.cdp.evaluate(
            "(() => { document.getElementById('username').value = %s;"
            " document.getElementById('password').value = %s;"
            " document.getElementById('login-form')"
            ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
            % (json.dumps(username), json.dumps(password))
        )
        await asyncio.sleep(1.5)
        await self.goto("/")
        await self.cdp.wait_for(
            "document.getElementById('app') && !document.getElementById('app').hidden", 20
        )


@pytest.fixture()
def hub(tmp_path: Path):
    data_dir = tmp_path / "users"
    data_dir.mkdir()
    server = LiveServer(data_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_login_shows_a_username_field(hub):
    """The login page must offer a username once there is more than one account."""
    client = hub.login()
    try:
        assert client.post("/api/users", json={
            "username": "anna", "password": "anna-password", "role": "user",
        }).status_code == 201
    finally:
        client.close()

    async def check():
        async with Browser(hub.url) as browser:
            await browser.goto("/")
            await browser.cdp.wait_for("!!document.getElementById('username')")
            assert await browser.cdp.evaluate("!!document.getElementById('password')")
            # Without a username the server asks for one.
            await browser.cdp.evaluate(
                "(() => { document.getElementById('username').value = '';"
                " document.getElementById('password').value = 'anna-password';"
                " document.getElementById('login-form')"
                ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
            )
            await asyncio.sleep(0.8)
            message = await browser.cdp.evaluate(
                "(document.getElementById('login-error')||{}).textContent || ''"
            )
            assert "username" in message.lower(), message

    asyncio.run(check())


def test_user_can_sign_in_with_a_username(hub):
    client = hub.login()
    try:
        client.post("/api/users", json={
            "username": "anna", "password": "anna-password", "role": "user",
        })
    finally:
        client.close()

    async def check():
        async with Browser(hub.url) as browser:
            await browser.login("anna-password", "anna")
            me = await browser.cdp.evaluate(
                "(async () => (await (await fetch('/api/me', {credentials:'same-origin'})).json()))()"
            )
            assert me["user"]["username"] == "anna"
            assert me["user"]["role"] == "user"
            # A normal user must not see the Users tab.
            await browser.cdp.evaluate("document.getElementById('btn-menu').click()")
            await browser.cdp.wait_for("document.getElementById('dialog').open")
            assert await browser.cdp.evaluate(
                "!document.querySelector('#dialog [data-tab=users]')"
            ), "normal users must not see the Users tab"
            # But they do see the Account tab for their own password.
            assert await browser.cdp.evaluate(
                "!!document.querySelector('#dialog [data-tab=account]')"
            )

    asyncio.run(check())


def test_admin_creates_a_user_from_the_users_tab(hub):
    async def check():
        async with Browser(hub.url) as browser:
            await browser.login(PASSWORD, "admin")
            await browser.cdp.evaluate("document.getElementById('btn-menu').click()")
            await browser.cdp.wait_for("document.getElementById('dialog').open")
            await browser.cdp.wait_for("!!document.querySelector('#dialog [data-tab=users]')")
            await browser.cdp.evaluate("document.querySelector('#dialog [data-tab=users]').click()")
            await browser.cdp.wait_for("!!document.querySelector('#dialog [data-new-user=username]')")
            await asyncio.sleep(0.5)

            listed = await browser.cdp.evaluate(
                "[...document.querySelectorAll('#dialog [data-user]')].map(e => e.dataset.user)"
            )
            assert "admin" in listed, listed

            await browser.cdp.evaluate(
                """(() => {
                    const root = document.getElementById('dialog-body');
                    root.querySelector('[data-new-user=username]').value = 'bruno';
                    root.querySelector('[data-new-user=password]').value = 'bruno-password';
                    root.querySelector('[data-new-user=display_name]').value = 'Bruno';
                    root.querySelector('[data-user-create]').click();
                })()"""
            )
            await asyncio.sleep(1.5)
            listed = await browser.cdp.evaluate(
                "[...document.querySelectorAll('#dialog [data-user]')].map(e => e.dataset.user)"
            )
            assert "bruno" in listed, "the new user should appear in the list: %s" % listed

    asyncio.run(check())

    # And the account really works.
    from fastapi.testclient import TestClient
    import httpx

    with httpx.Client(base_url=hub.url, timeout=10) as fresh:
        assert fresh.post("/api/login", json={
            "username": "bruno", "password": "bruno-password"}).status_code == 200


def test_setup_page_on_an_empty_hub(tmp_path: Path):
    """A fresh install lands on /setup and creates the first admin."""
    import threading
    import time

    import uvicorn

    from smarthome.config import Config
    from smarthome.server import create_app

    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    config = Config.load(data_dir / "config.json", bootstrap_password=False)
    config.save()

    app = create_app(str(config.path))

    from tests.test_e2e import free_port

    port = free_port()
    config_obj = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                ws_ping_interval=None, ws_ping_timeout=None)
    server = uvicorn.Server(config_obj)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)

    url = "http://127.0.0.1:%d" % port

    async def check():
        async with Browser(url) as browser:
            await browser.goto("/")
            await browser.cdp.wait_for("!!document.getElementById('setup-form')", 20)
            await browser.cdp.evaluate(
                """(() => {
                    document.getElementById('username').value = 'owner';
                    document.getElementById('password').value = 'owner-password';
                    document.getElementById('confirm').value = 'owner-password';
                    document.getElementById('setup-form')
                      .dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
                })()"""
            )
            await asyncio.sleep(2.0)
            await browser.goto("/")
            await browser.cdp.wait_for(
                "document.getElementById('app') && !document.getElementById('app').hidden", 20
            )

    try:
        asyncio.run(check())

        # Still running here: confirm the new account works.
        import httpx

        with httpx.Client(base_url=url, timeout=10) as client:
            assert client.post("/api/login", json={
                "username": "owner", "password": "owner-password"}).status_code == 200
            # And setup cannot be repeated.
            assert client.get("/setup", follow_redirects=False).status_code == 303
    finally:
        server.should_exit = True
        thread.join(timeout=10)
