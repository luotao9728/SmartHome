"""Check layout geometry in a real browser: no overlaps, sane sizes, no clipping.

    python scripts/ui_layout.py http://127.0.0.1:8099 --key shk_...
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

GEOMETRY = r"""
(() => {
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const cards = Array.from(document.querySelectorAll('.card')).map(c => ({
    uid: c.dataset.uid,
    ...rect(c),
    clipped: c.scrollWidth > c.clientWidth + 1,
    overflowing: c.scrollHeight > c.clientHeight + 1,
    controls: Array.from(c.querySelectorAll('[data-control-row]')).map(row => ({
      label: (row.querySelector('.switch-label') || row.querySelector('.control-label span'))
        ? (row.querySelector('.switch-label') || row.querySelector('.control-label span')).textContent
        : '',
      ...rect(row),
      clipped: row.scrollWidth > row.clientWidth + 1,
    })),
  }));
  const overlaps = [];
  for (let i = 0; i < cards.length; i += 1) {
    for (let j = i + 1; j < cards.length; j += 1) {
      const a = cards[i], b = cards[j];
      const hx = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const hy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      if (hx > 2 && hy > 2) overlaps.push([a.uid, b.uid, hx, hy]);
    }
  }
  const wide = Array.from(document.querySelectorAll('body *')).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.right > window.innerWidth + 1;
  }).map(el => el.tagName + '.' + (el.className || '').toString().slice(0, 30));
  return {
    viewport: {w: window.innerWidth, h: window.innerHeight},
    doc: {w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight},
    header: rect(document.querySelector('.topbar')),
    overview: rect(document.querySelector('.overview')),
    cards,
    overlaps,
    clippedCards: cards.filter(c => c.clipped).map(c => c.uid),
    overflowingCards: cards.filter(c => c.overflowing).map(c => c.uid),
    clippedControls: cards.flatMap(c => c.controls.filter(r => r.clipped).map(r => c.uid + ':' + r.label)),
    elementsBeyondViewport: wide.slice(0, 8),
    footer: rect(document.querySelector('.footer')),
  };
})()
"""


async def check(url: str, key: str = "", password: str = "", width: int = 1280, height: int = 880) -> int:
    from websockets.asyncio.client import connect

    port = free_port()
    profile = tempfile.mkdtemp(prefix="sh-layout-")
    process = subprocess.Popen(
        [find_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures = []
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
                           {"width": width, "height": height, "deviceScaleFactor": 1,
                            "mobile": width < 600})
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
            await asyncio.sleep(0.6)

            geometry = await cdp.evaluate(GEOMETRY)
            print(json.dumps(geometry, indent=2))

            if geometry["overlaps"]:
                failures.append("overlapping cards: %s" % geometry["overlaps"])
            if geometry["clippedCards"]:
                failures.append("clipped cards: %s" % geometry["clippedCards"])
            if geometry["clippedControls"]:
                failures.append("clipped controls: %s" % geometry["clippedControls"])
            if geometry["elementsBeyondViewport"]:
                failures.append("elements past the viewport: %s" % geometry["elementsBeyondViewport"])
            if geometry["doc"]["w"] > geometry["viewport"]["w"] + 1:
                failures.append("horizontal scroll: doc=%s viewport=%s"
                                % (geometry["doc"]["w"], geometry["viewport"]["w"]))
            for card in geometry["cards"]:
                if card["w"] < 200:
                    failures.append("card %s is only %spx wide" % (card["uid"], card["w"]))
                if card["h"] < 90:
                    failures.append("card %s is only %spx tall" % (card["uid"], card["h"]))
            if geometry["header"] and geometry["overview"]:
                if geometry["overview"]["y"] < geometry["header"]["y"] + geometry["header"]["h"] - 2:
                    failures.append("overview overlaps the header")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)

    if failures:
        print("\nLAYOUT PROBLEMS:")
        for problem in failures:
            print("  - %s" % problem)
        return 1
    print("\nlayout ok (%d cards)" % len(geometry["cards"]))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the UI layout geometry")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8080")
    parser.add_argument("--key", default=os.environ.get("SMARTHOME_UI_KEY", ""))
    parser.add_argument("--password", default=os.environ.get("SMARTHOME_UI_PASSWORD", ""))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=880)
    args = parser.parse_args(argv)
    return asyncio.run(check(args.url, args.key, args.password, args.width, args.height))


if __name__ == "__main__":
    sys.exit(main())
