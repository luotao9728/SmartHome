"""Regression test: every Settings tab must open without closing the dialog.

The Settings dialog is a <form method="dialog">, so any <button> inside it
without an explicit type defaults to type="submit" and dismisses the dialog.
That made every tab except the initial one impossible to reach.
"""

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

#: Every tab the admin should be able to reach.
SETTINGS_TABS = ["network", "nodes", "types", "account", "users"]


@pytest.fixture()
def hub(tmp_path: Path):
    data_dir = tmp_path / "tabs"
    data_dir.mkdir()
    server = LiveServer(data_dir)
    server.start()
    try:
        # One device so the device dialog has a card to open.
        client = server.login()
        try:
            client.post("/api/devices", json={
                "type": "relay", "uid": "tab-relay", "name": "Tab relay",
            })
        finally:
            client.close()
        yield server
    finally:
        server.stop()


def _run_checks(hub: LiveServer) -> None:
    from ui_test import CDP, find_browser, free_port, http_json
    from websockets.asyncio.client import connect

    async def drive() -> None:
        port = free_port()
        profile = tempfile.mkdtemp(prefix="sh-tabs-")
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
                    "(() => { document.getElementById('username').value = 'admin';"
                    " document.getElementById('password').value = %s;"
                    " document.getElementById('login-form')"
                    ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
                    % json.dumps(PASSWORD)
                )
                await asyncio.sleep(1.5)
                await cdp.send("Page.navigate", {"url": hub.url})
                await cdp.wait_for(
                    "document.getElementById('app') && !document.getElementById('app').hidden", 20
                )

                # --- Settings dialog: click every tab in turn ------------- #
                await cdp.evaluate("document.getElementById('btn-menu').click()")
                await cdp.wait_for("document.getElementById('dialog').open")

                for tab in SETTINGS_TABS:
                    await cdp.evaluate(
                        "document.querySelector('#dialog [data-tab=%s]').click()" % tab
                    )
                    await asyncio.sleep(0.25)
                    still_open = await cdp.evaluate("document.getElementById('dialog').open")
                    assert still_open, (
                        "the Settings dialog closed when the %r tab was clicked" % tab
                    )
                    visible = await cdp.evaluate(
                        "!document.querySelector('#dialog [data-panel=%s]').hidden" % tab
                    )
                    assert visible, "the %r panel did not become visible" % tab
                await cdp.evaluate("document.getElementById('dialog').close()")

                # --- Device dialog: same problem would hide its tabs ------ #
                await cdp.evaluate(
                    "document.querySelector('.card [data-menu]').click()"
                )
                await cdp.wait_for("document.getElementById('dialog').open")
                for tab in ("general", "hardware", "advanced"):
                    await cdp.evaluate(
                        "document.querySelector('#dialog [data-tab=%s]').click()" % tab
                    )
                    await asyncio.sleep(0.15)
                    assert await cdp.evaluate("document.getElementById('dialog').open"), (
                        "the device dialog closed on the %r tab" % tab
                    )
                await cdp.evaluate("document.getElementById('dialog').close()")

                # --- Controls inside the dialog must not submit either ---- #
                await cdp.evaluate("document.getElementById('btn-menu').click()")
                await cdp.wait_for("document.getElementById('dialog').open")
                await cdp.evaluate(
                    "document.querySelector('#dialog [data-tab=nodes]').click()"
                )
                await asyncio.sleep(0.3)
                has_enroll = await cdp.evaluate(
                    "!!document.querySelector('#dialog [data-enroll]')"
                )
                assert has_enroll, "the Nodes panel should offer an enrol button"
                assert await cdp.evaluate("document.getElementById('dialog').open")
                await cdp.evaluate("document.getElementById('dialog').close()")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(profile, ignore_errors=True)

    asyncio.run(drive())


def test_settings_tabs_do_not_close_the_dialog(hub: LiveServer):
    _run_checks(hub)


def test_no_dialog_button_defaults_to_submit():
    """Static guard: every <button> in app.js must declare its type."""
    import re

    source = (ROOT / "smarthome" / "web" / "app.js").read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r"<button\b([^>]*)>", source):
        if "type=" not in match.group(1):
            line = source[: match.start()].count("\n") + 1
            offenders.append((line, match.group(1).strip()[:60]))
    assert not offenders, "buttons without an explicit type: %s" % offenders
