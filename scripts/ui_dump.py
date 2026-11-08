"""Dump what the UI actually renders, as text (a stand-in for a screenshot).

    python scripts/ui_dump.py http://127.0.0.1:8099 --key shk_...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_test import CDP, find_browser, free_port, http_json  # noqa: E402


async def dump(url: str, key: str = "", password: str = "", width: int = 1280, height: int = 880) -> None:
    from websockets.asyncio.client import connect

    port = free_port()
    profile = tempfile.mkdtemp(prefix="sh-dump-")
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
        page = next(t for t in http_json("http://127.0.0.1:%d/json/list" % port) if t["type"] == "page")
        async with connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as websocket:
            cdp = CDP(websocket, "")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.enable")
            await cdp.send("Emulation.setDeviceMetricsOverride",
                           {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            target = (url.rstrip("/") + "/auth?key=" + key) if key else url
            await cdp.send("Page.navigate", {"url": target})
            await asyncio.sleep(1.2)
            if password:
                await cdp.evaluate(
                    "(() => { document.getElementById('password').value = %s;"
                    " document.getElementById('login-form')"
                    ".dispatchEvent(new Event('submit', {cancelable: true, bubbles: true})); })()"
                    % json.dumps(password)
                )
                await asyncio.sleep(1.2)
                await cdp.send("Page.navigate", {"url": url})
                await asyncio.sleep(1.2)
            await cdp.wait_for(
                "document.getElementById('app') && !document.getElementById('app').hidden", 20
            )
            await asyncio.sleep(0.5)

            print("URL:", await cdp.evaluate("location.href"))
            print("TITLE:", await cdp.evaluate("document.title"))
            print("STATUS:", await cdp.evaluate(
                "document.getElementById('conn-status').textContent.trim()"))
            print("STATS:", await cdp.evaluate(
                "['stat-devices','stat-online','stat-nodes']"
                ".map(id => id.split('-')[1] + '=' + document.getElementById(id).textContent).join(' ')"))
            print()
            cards = await cdp.evaluate("""(() => Array.from(document.querySelectorAll('.card')).map(card => ({
                room: card.closest('.room') ? card.closest('.room').querySelector('h2').textContent : '',
                name: card.querySelector('[data-name]').textContent,
                sub: card.querySelector('[data-sub]').textContent,
                badge: card.querySelector('[data-badge]').textContent,
                on: card.classList.contains('on'),
                controls: Array.from(card.querySelectorAll('[data-prop], [data-readout]')).map(el => {
                    const row = el.closest('[data-control-row]');
                    const labelEl = row
                        ? (row.querySelector('.control-label span') || row.querySelector('.switch-label'))
                        : null;
                    const value = el.dataset.kind === 'bool'
                        ? el.getAttribute('aria-checked')
                        : (el.dataset.readout !== undefined
                            ? el.textContent
                            : (el.value !== undefined ? el.value : ''));
                    return (labelEl ? labelEl.textContent : (el.dataset.prop || 'value')) + '=' + value;
                }),
                actions: Array.from(card.querySelectorAll('[data-action]')).map(b => b.textContent),
                error: card.querySelector('.card-error') ? card.querySelector('.card-error .grow').textContent : '',
            })))()""")
            current_room = None
            for card in cards:
                if card["room"] != current_room:
                    current_room = card["room"]
                    print("[%s]" % (current_room or "Other"))
                flag = "*" if card["on"] else " "
                print("  %s %-18s %-34s %-14s %s" % (
                    flag, card["name"], card["sub"], card["badge"], " · ".join(card["controls"])))
                if card["actions"]:
                    print("      actions: %s" % ", ".join(card["actions"]))
                if card["error"]:
                    print("      error: %s" % card["error"])
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print what the UI renders")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8080")
    parser.add_argument("--key", default=os.environ.get("SMARTHOME_UI_KEY", ""))
    parser.add_argument("--password", default=os.environ.get("SMARTHOME_UI_PASSWORD", ""))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=880)
    args = parser.parse_args(argv)
    asyncio.run(dump(args.url, args.key, args.password, args.width, args.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
