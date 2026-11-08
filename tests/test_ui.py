"""Browser UI tests (headless Edge/Chrome over the DevTools protocol).

These are skipped automatically when no Chromium-based browser is available, so
the suite still runs on a bare Raspberry Pi.  They exercise what unit tests
cannot: that the generated controls actually talk to the API, that the dialogs
render from the manifests, and that the page has no console errors or layout
overflow -- on a phone-sized viewport too.

    python -m pytest tests/test_ui.py -q
    SMARTHOME_UI_BROWSER=0 python -m pytest -q     # skip explicitly
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
except Exception:  # pragma: no cover - scripts always present in the repo
    find_browser = None  # type: ignore[assignment]

from tests.test_e2e import LiveServer, free_port  # noqa: E402


def _browser_available() -> bool:
    if os.environ.get("SMARTHOME_UI_BROWSER") == "0":
        return False
    if find_browser is None:
        return False
    try:
        find_browser()
        return True
    except SystemExit:
        return False


pytestmark = pytest.mark.skipif(
    not _browser_available(),
    reason="no Chromium based browser available (set SMARTHOME_UI_BROWSER=0 to silence)",
)


@pytest.fixture()
def live_hub(tmp_path: Path):
    data_dir = tmp_path / "ui"
    data_dir.mkdir()
    server = LiveServer(data_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _seed(server: LiveServer) -> None:
    """Create a couple of devices so the cards have something to show."""
    client = server.login()
    try:
        client.post("/api/enroll", json={"seconds": 60})
        for payload in (
            {"type": "pwm_led_strip", "uid": "ui-strip", "name": "Desk strip",
             "room": "Office", "config": {"pin": 4}},
            {"type": "relay", "uid": "ui-relay", "name": "Pump",
             "room": "Utility", "config": {"pin": 5}},
            {"type": "temperature_sensor", "uid": "ui-temp", "name": "Room",
             "room": "Hall", "config": {"pin": 6, "interval_s": 60}},
        ):
            response = client.post("/api/devices", json=payload)
            assert response.status_code == 201, response.text
        client.post("/api/devices/ui-strip/props", json={"props": {"power": True, "brightness": 70}})
    finally:
        client.close()


@pytest.mark.parametrize("viewport", [(1280, 900), (390, 844)], ids=["desktop", "phone"])
def test_ui_against_a_live_hub(live_hub: LiveServer, viewport, tmp_path: Path):
    import asyncio

    from ui_test import run

    _seed(live_hub)
    width, height = viewport
    exit_code = asyncio.run(run(
        live_hub.url,
        password="e2e-password",
        width=width,
        height=height,
        mobile=width < 600,
    ))
    assert exit_code == 0, "UI checks failed (see the printed report)"


@pytest.mark.parametrize("viewport", [(1280, 900), (390, 844), (1920, 1080)],
                         ids=["desktop", "phone", "wide"])
def test_ui_layout_has_no_overlaps_or_clipping(live_hub: LiveServer, viewport):
    """Cards must not overlap, clip their controls, or overflow the viewport."""
    import asyncio

    import ui_layout

    _seed(live_hub)
    width, height = viewport
    exit_code = asyncio.run(ui_layout.check(
        live_hub.url, password="e2e-password", width=width, height=height
    ))
    assert exit_code == 0, "layout problems (see the printed geometry)"


def test_ui_empty_hub_shows_onboarding(live_hub: LiveServer):
    import asyncio

    from ui_test import run

    exit_code = asyncio.run(run(live_hub.url, password="e2e-password", width=1024, height=800))
    assert exit_code == 0, "empty-state UI checks failed"
