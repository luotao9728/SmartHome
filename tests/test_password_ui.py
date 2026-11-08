"""End-to-end test for changing the admin password from the web UI.

Covers the whole path: the Account tab renders, the form validates, POST
/api/password updates the config the service reads, and the new password is the
one that works afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from ui_test import find_browser
except Exception:  # pragma: no cover
    find_browser = None  # type: ignore[assignment]

from tests.test_e2e import PASSWORD, LiveServer, free_port  # noqa: E402


def _browser_available() -> bool:
    if os.environ.get("SMARTHOME_UI_BROWSER") == "0":
        return False
    try:
        find_browser()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="no browser available")

NEW_PASSWORD = "a-much-better-password"


@pytest.fixture()
def hub(tmp_path: Path):
    data_dir = tmp_path / "pwui"
    data_dir.mkdir()
    server = LiveServer(data_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _login_status(url: str, password: str) -> int:
    import httpx

    with httpx.Client(base_url=url, timeout=10) as client:
        return client.post("/api/login", json={"password": password}).status_code


def test_password_change_via_api_updates_the_login(hub: LiveServer):
    """The API itself must accept the new password and reject the old one."""
    client = hub.login()
    try:
        response = client.post("/api/password",
                               json={"current": PASSWORD, "new": NEW_PASSWORD})
        assert response.status_code == 200, response.text
    finally:
        client.close()

    assert _login_status(hub.url, NEW_PASSWORD) == 200
    assert _login_status(hub.url, PASSWORD) == 401


def test_api_rejects_wrong_current_password(hub: LiveServer):
    client = hub.login()
    try:
        response = client.post("/api/password",
                               json={"current": "not-the-password", "new": NEW_PASSWORD})
        assert response.status_code == 403
    finally:
        client.close()
    # Nothing changed.
    assert _login_status(hub.url, PASSWORD) == 200


def test_api_rejects_short_password(hub: LiveServer):
    client = hub.login()
    try:
        response = client.post("/api/password", json={"current": PASSWORD, "new": "short"})
        assert response.status_code == 400
    finally:
        client.close()
    assert _login_status(hub.url, PASSWORD) == 200


def test_account_tab_changes_the_password(hub: LiveServer):
    """Drive the real UI: open Settings, fill the Account form, submit."""
    from ui_test import CDP, find_browser, free_port as port_of, http_json
    from websockets.asyncio.client import connect

    async def drive() -> None:
        port = port_of()
        profile = tempfile.mkdtemp(prefix="sh-pwui-")
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
                await cdp.send("Page.navigate", {"url": hub.url})
                await asyncio.sleep(1.0)
                await cdp.evaluate(
                    "(() => { document.getElementById('password').value = %s;"
                    " document.getElementById('login-form')"
                    ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
                    % json.dumps(PASSWORD)
                )
                await asyncio.sleep(1.5)
                await cdp.send("Page.navigate", {"url": hub.url})
                await cdp.wait_for(
                    "document.getElementById('app') && !document.getElementById('app').hidden", 20
                )

                # Open Settings -> Account.
                await cdp.evaluate("document.getElementById('btn-menu').click()")
                await cdp.wait_for("document.getElementById('dialog').open")
                await cdp.wait_for("!!document.querySelector('#dialog [data-tab=account]')")
                await cdp.evaluate("document.querySelector('#dialog [data-tab=account]').click()")
                await cdp.wait_for("!!document.querySelector('#dialog [data-password-form]')")
                assert await cdp.evaluate(
                    "!document.querySelector('#dialog [data-panel=account]').hidden"
                ), "the Account panel should be visible"

                # Mismatched confirmation must be caught client-side.
                await cdp.evaluate(
                    """(() => {
                        const f = document.querySelector('#dialog [data-password-form]');
                        f.querySelector('[data-password=current]').value = %s;
                        f.querySelector('[data-password=new]').value = %s;
                        f.querySelector('[data-password=confirm]').value = 'something-else';
                        f.querySelector('[data-password-submit]').click();
                    })()""" % (json.dumps(PASSWORD), json.dumps(NEW_PASSWORD))
                )
                await asyncio.sleep(0.3)
                message = await cdp.evaluate(
                    "(document.querySelector('#dialog [data-password-error]')||{}).textContent || ''"
                )
                assert "do not match" in message, "mismatch should be reported: %r" % message

                # Now submit properly.
                await cdp.evaluate(
                    """(() => {
                        const f = document.querySelector('#dialog [data-password-form]');
                        f.querySelector('[data-password=current]').value = %s;
                        f.querySelector('[data-password=new]').value = %s;
                        f.querySelector('[data-password=confirm]').value = %s;
                        f.querySelector('[data-password-submit]').click();
                    })()""" % (json.dumps(PASSWORD), json.dumps(NEW_PASSWORD),
                                 json.dumps(NEW_PASSWORD))
                )
                await asyncio.sleep(1.5)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(profile, ignore_errors=True)

    asyncio.run(drive())

    # The change must have reached the config the service reads.
    assert _login_status(hub.url, NEW_PASSWORD) == 200
    assert _login_status(hub.url, PASSWORD) == 401
