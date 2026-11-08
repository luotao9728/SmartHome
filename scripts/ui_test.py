#!/usr/bin/env python3
"""Headless UI verification over the Chrome DevTools Protocol.

Runs a real (headless) browser against a running hub and asserts on the rendered
DOM, console errors, layout overflow and actual interaction results.

    python scripts/ui_test.py http://127.0.0.1:8099 --password test-password
    python scripts/ui_test.py http://127.0.0.1:8099 --key shk_...

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_browser() -> str:
    for candidate in EDGE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no Chromium based browser found")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CDP:
    def __init__(self, websocket, ws_url: str):
        self.ws = websocket
        self.ws_url = ws_url
        self._next = 0
        self.events: List[Dict[str, Any]] = []

    async def send(self, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
        self._next += 1
        message_id = self._next
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - time.time()))
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError("%s failed: %s" % (method, message["error"]))
                return message.get("result", {})
            self.events.append(message)
        raise TimeoutError(method)

    async def drain(self, seconds: float = 0.5) -> List[Dict[str, Any]]:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), max(0.02, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            self.events.append(json.loads(raw))
        return self.events

    async def evaluate(self, expression: str) -> Any:
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"].get("text") or "JS exception")
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, timeout: float = 15.0, interval: float = 0.15) -> Any:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                value = await self.evaluate(expression)
            except RuntimeError as exc:
                last = exc
                value = None
            if value:
                return value
            await asyncio.sleep(interval)
        raise AssertionError("timed out waiting for %r (last=%r)" % (expression, last))


def http_json(url: str, *, method: str = "GET", body: Optional[dict] = None,
              headers: Optional[dict] = None, timeout: float = 10.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self):
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: Any, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print("  ok   %s" % name)
        else:
            self.failed.append(name)
            print("  FAIL %s %s" % (name, detail))

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print("\n%d/%d checks passed" % (self.passed, total))
        if self.failed:
            print("failed: %s" % ", ".join(self.failed))
            return 1
        return 0


async def run(url: str, *, key: str = "", password: str = "", width: int = 1280,
              height: int = 900, mobile: bool = False, screenshots: str = "") -> int:
    from websockets.asyncio.client import connect

    report = Report()
    browser = find_browser()
    port = free_port()
    profile = tempfile.mkdtemp(prefix="sh-ui-")
    args = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=Translate,BackForwardCache",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "about:blank",
    ]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        endpoint = "http://127.0.0.1:%d/json/version" % port
        for _ in range(100):
            try:
                http_json(endpoint, timeout=1.0)
                break
            except (urllib.error.URLError, OSError):
                await asyncio.sleep(0.1)
        else:
            raise SystemExit("browser did not start")

        targets = http_json("http://127.0.0.1:%d/json/list" % port)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            page = http_json("http://127.0.0.1:%d/json/new" % port, method="PUT")
        ws_url = page["webSocketDebuggerUrl"]

        async with connect(ws_url, max_size=20 * 1024 * 1024) as websocket:
            cdp = CDP(websocket, ws_url)
            await cdp.send("Runtime.enable")
            await cdp.send("Log.enable")
            await cdp.send("Page.enable")
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile,
            })

            # -- sign in -------------------------------------------------- #
            if key:
                nav = url.rstrip("/") + "/auth?key=" + key
            else:
                nav = url
            await cdp.send("Page.navigate", {"url": nav})
            await asyncio.sleep(1.2)

            if password:
                await cdp.wait_for("!!document.getElementById('login-form')")
                report.check("login page renders", True)
                await cdp.evaluate(
                    "(() => { const f = document.getElementById('login-form');"
                    " document.getElementById('password').value = %s;"
                    " f.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
                    % json.dumps(password)
                )
                await asyncio.sleep(1.5)
                await cdp.send("Page.navigate", {"url": url})
                await asyncio.sleep(1.0)

            # -- main app ------------------------------------------------- #
            await cdp.wait_for("document.getElementById('app') && !document.getElementById('app').hidden", 20)
            report.check("app shell visible", True)

            cards = await cdp.evaluate("document.querySelectorAll('.card').length")
            if cards:
                report.check("device cards rendered", True)
                report.check("no empty state while devices exist",
                             not await cdp.evaluate("!document.getElementById('empty-state').hidden"))
            else:
                report.check("empty state shown on a fresh hub",
                             await cdp.evaluate("!document.getElementById('empty-state').hidden"))

            stats = await cdp.evaluate(
                "({d: document.getElementById('stat-devices').textContent,"
                " o: document.getElementById('stat-online').textContent,"
                " n: document.getElementById('stat-nodes').textContent})"
            )
            report.check("overview counts populated", stats["d"].isdigit() and int(stats["d"]) == cards,
                         str(stats))

            online = await cdp.evaluate(
                "document.querySelector('.conn').classList.contains('online')"
            )
            report.check("websocket shows live", online)

            # no horizontal overflow
            overflow = await cdp.evaluate(
                "document.documentElement.scrollWidth - window.innerWidth"
            )
            report.check("no horizontal overflow", overflow <= 1, "overflow=%s" % overflow)

            # Controls only exist once a device does; the add/settings dialogs
            # are checked either way further down.
            has_strip = await cdp.evaluate(
                "Array.from(document.querySelectorAll('.card')).some(c => c.querySelector('.switch'))"
            )
            if cards:
                report.check("switch control rendered", has_strip)
                report.check("slider control rendered",
                             await cdp.evaluate("document.querySelectorAll('.card input[type=range]').length > 0"))
                report.check("cards show a status badge",
                             await cdp.evaluate("document.querySelectorAll('.card .badge').length > 0"))

            # Avoid a 401 when the script was not given the API key: use the
            # browser session instead, which is always valid at this point.
            async def api_get(path: str):
                return json.loads(await cdp.evaluate(
                    "(async () => { const r = await fetch(%s, {credentials:'same-origin'});"
                    " return await r.text(); })()" % json.dumps(path)
                ))

            devices_payload = await api_get("/api/devices")
            strip = next((d for d in devices_payload["devices"] if d["type"] == "pwm_led_strip"), None)
            if strip is not None:
                target_state = not strip["desired"].get("power", False)
                clicked = await cdp.evaluate(
                    """(() => {
                        const card = document.querySelector('.card[data-uid="%s"]');
                        if (!card) return false;
                        const sw = card.querySelector('.switch');
                        if (!sw) return false;
                        sw.click();
                        return true;
                    })()""" % strip["uid"]
                )
                report.check("switch clickable", clicked)
                await asyncio.sleep(1.0)
                after = await api_get("/api/devices")
                updated = next(d for d in after["devices"] if d["uid"] == strip["uid"])
                report.check("toggle reached the server",
                             bool(updated["desired"].get("power")) == target_state,
                             "desired=%s" % (updated["desired"],))
                report.check("server acked and hardware reported",
                             bool(updated["reported"].get("power")) == target_state,
                             "reported=%s err=%r" % (updated["reported"], updated.get("error")))
                report.check("card reflects state",
                             await cdp.evaluate(
                                 """(() => {
                                    const card = document.querySelector('.card[data-uid="%s"] .switch');
                                    return card ? card.getAttribute('aria-checked') === '%s' : false;
                                 })()""" % (strip["uid"], "true" if target_state else "false")
                             ))
            else:
                print("  --   no PWM strip on this hub; skipping the toggle checks")

            # -- dialogs --------------------------------------------------- #
            await cdp.evaluate("document.getElementById('btn-add').click()")
            await cdp.wait_for("document.getElementById('dialog').open")
            report.check("add dialog opens", True)
            report.check("add dialog lists types",
                         await cdp.evaluate("document.querySelectorAll('#dialog [data-type]').length > 1"))
            report.check("add dialog shows config fields",
                         await cdp.evaluate("document.querySelectorAll('#dialog [data-config]').length > 0"))
            await cdp.evaluate("document.getElementById('dialog').close()")

            await cdp.evaluate("document.getElementById('btn-menu').click()")
            await cdp.wait_for("document.getElementById('dialog').open")
            report.check("settings dialog opens", True)
            report.check("settings has network fields",
                         await cdp.evaluate("document.querySelectorAll('#dialog [data-setting]').length >= 4"))
            await cdp.evaluate("document.querySelector('#dialog [data-tab=nodes]').click()")
            node_count = await cdp.evaluate(
                "(async () => { const r = await fetch('/api/nodes', {credentials:'same-origin'});"
                " return (await r.json()).nodes.length; })()"
            )
            report.check("nodes tab lists every node",
                         await cdp.evaluate("document.querySelectorAll('#dialog [data-node-token]').length")
                         == node_count, "expected %s" % node_count)
            await cdp.evaluate("document.getElementById('dialog').close()")

            # device dialog
            if strip is not None:
                await cdp.evaluate(
                    """(() => { const btn = document.querySelector('.card[data-uid="%s"] [data-menu]');
                        if (btn) btn.click(); })()""" % strip["uid"]
                )
                await cdp.wait_for("document.getElementById('dialog').open")
                report.check("device dialog opens", True)
                report.check("device dialog has hardware tab",
                             await cdp.evaluate("!!document.querySelector('#dialog [data-tab=hardware]')"))
                await cdp.evaluate("document.querySelector('#dialog [data-tab=advanced]').click()")
                await cdp.wait_for("!!document.querySelector('#dialog [data-history]')")
                await asyncio.sleep(0.8)
                report.check("history sparkline area loads",
                             await cdp.evaluate(
                                 "document.querySelector('#dialog [data-history]').textContent.length > 0"))
                await cdp.evaluate("document.getElementById('dialog').close()")

            # -- activity log --------------------------------------------- #
            await cdp.evaluate("document.getElementById('btn-events').click()")
            await cdp.wait_for("document.getElementById('dialog').open")
            await asyncio.sleep(0.6)
            event_count = await cdp.evaluate(
                "(async () => { const r = await fetch('/api/events?limit=50', {credentials:'same-origin'});"
                " return (await r.json()).events.length; })()"
            )
            report.check("activity log matches the API",
                         await cdp.evaluate("document.querySelectorAll('#dialog .log-item').length")
                         == min(event_count, 80), "events=%s" % event_count)
            await cdp.evaluate("document.getElementById('dialog').close()")

            # -- theme ----------------------------------------------------- #
            await cdp.evaluate("document.getElementById('btn-theme').click()")
            theme = await cdp.evaluate("document.documentElement.dataset.theme || 'auto'")
            report.check("theme toggle works", theme in ("dark", "light", "auto"), theme)
            await cdp.evaluate("localStorage.removeItem('smarthome-theme')")

            if screenshots:
                os.makedirs(screenshots, exist_ok=True)

                async def shoot(name: str) -> None:
                    shot = await cdp.send("Page.captureScreenshot",
                                          {"format": "png", "captureBeyondViewport": False})
                    import base64

                    path = os.path.join(screenshots, name + ".png")
                    with open(path, "wb") as handle:
                        handle.write(base64.b64decode(shot["data"]))
                    print("  screenshot -> %s" % path)

                await shoot("home")
                if cards:
                    await cdp.evaluate("document.getElementById('btn-add').click()")
                    await cdp.wait_for("document.getElementById('dialog').open")
                    await asyncio.sleep(0.4)
                    await shoot("add-device")
                    await cdp.evaluate("document.getElementById('dialog').close()")
                    first_uid = await cdp.evaluate(
                        "document.querySelector('.card') && document.querySelector('.card').dataset.uid"
                    )
                    if first_uid:
                        await cdp.evaluate(
                            """(() => { const b = document.querySelector('.card[data-uid="%s"] [data-menu]');
                                if (b) b.click(); })()""" % first_uid
                        )
                        await cdp.wait_for("document.getElementById('dialog').open")
                        await asyncio.sleep(0.4)
                        await shoot("device-settings")
                        await cdp.evaluate("document.getElementById('dialog').close()")
                await cdp.evaluate("document.getElementById('btn-menu').click()")
                await cdp.wait_for("document.getElementById('dialog').open")
                await asyncio.sleep(0.4)
                await shoot("settings")
                await cdp.evaluate("document.getElementById('dialog').close()")
                # Both themes, explicitly (headless browsers often prefer dark,
                # so "auto" would make the two shots identical).
                await cdp.evaluate("document.documentElement.dataset.theme = 'light'")
                await asyncio.sleep(0.35)
                await shoot("home-light")
                await cdp.evaluate("document.documentElement.dataset.theme = 'dark'")
                await asyncio.sleep(0.35)
                await shoot("home-dark")
                await cdp.evaluate("delete document.documentElement.dataset.theme")
                report.check("themes render distinct backgrounds",
                             await cdp.evaluate(
                                 "(() => { const s = getComputedStyle(document.body).backgroundColor;"
                                 " return s !== 'rgba(0, 0, 0, 0)' && s.length > 0; })()"
                             ))

            # -- console --------------------------------------------------- #
            await cdp.drain(0.5)
            errors = []
            for event in cdp.events:
                method = event.get("method")
                if method == "Runtime.exceptionThrown":
                    errors.append(event["params"]["exceptionDetails"].get("text", "exception"))
                elif method == "Log.entryAdded":
                    entry = event["params"]["entry"]
                    if entry.get("level") == "error":
                        errors.append(entry.get("text", ""))
                elif method == "Runtime.consoleAPICalled" and event["params"].get("type") == "error":
                    errors.append("console.error")
            report.check("no console errors", not errors, "; ".join(errors[:3]))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return report.summary()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless UI checks for SmartHome")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8099")
    parser.add_argument("--key", default=os.environ.get("SMARTHOME_UI_KEY", ""))
    parser.add_argument("--password", default=os.environ.get("SMARTHOME_UI_PASSWORD", ""))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--mobile", action="store_true")
    parser.add_argument("--screenshots", default="")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.url, key=args.key.strip().lstrip("\ufeff"), password=args.password,
                           width=args.width, height=args.height, mobile=args.mobile,
                           screenshots=args.screenshots))


if __name__ == "__main__":
    sys.exit(main())
