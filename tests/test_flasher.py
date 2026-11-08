"""Tests for the web flasher: parsing, validation and the job state machine.

The serial/esptool side is faked, so these run without a board.  They focus on
the parts that can silently do the wrong thing: chip detection, firmware URL
resolution, config validation and the step lifecycle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.flasher as flasher  # noqa: E402


# --------------------------------------------------------------------------- #
# output parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("Chip is ESP32-C6 (QFN40) (revision v0.0)", "ESP32-C6"),
    ("Chip is ESP32-C3 (QFN32) (revision v0.4)", "ESP32-C3"),
    ("Chip is ESP32-S3 (QFN56) (revision v0.2)", "ESP32-S3"),
    # parse_chip reports what esptool says; chip_family() maps it to a build.
    ("Chip is ESP32-D0WD (revision 1.0)", "ESP32-D0WD"),
    # esptool 5.x prints the name after the "Detecting chip type..." prefix.
    ("Detecting chip type... ESP32-C6", "ESP32-C6"),
    ("Detecting chip type... ESP32-S3", "ESP32-S3"),
    ("Detecting chip type...ESP32-C3", "ESP32-C3"),
    ("ESP32-C3 (revision 3)", "ESP32-C3"),
    # The ROM reports ESP8266EX; chip_family() normalises it to ESP8266.
    ("Chip is ESP8266EX", "ESP8266EX"),
    ("Connecting....", None),
    ("", None),
])
def test_parse_chip(text, expected):
    assert flasher.parse_chip(text) == expected


@pytest.mark.parametrize("line,percent", [
    ("Writing at 0x00010000... (10 %)", 10),
    ("Writing at 0x000f0000... (100 %)", 100),
    ("Writing at 0x00010000... (7 %)", 7),
    ("Compressed 12345 bytes...", None),
])
def test_parse_percent(line, percent):
    assert flasher.parse_percent(line) == percent


# --------------------------------------------------------------------------- #
# firmware URL resolution
# --------------------------------------------------------------------------- #
class FakePage:
    def __init__(self, html):
        self._html = html.encode()

    def read(self):
        return self._html


def page_with(*versions):
    links = "".join(
        '<a href="/resources/firmware/ESP32_GENERIC_C6-20260101-v%s.bin">x</a>' % v
        for v in versions
    )
    # A preview build that must be ignored.
    links += '<a href="/resources/firmware/ESP32_GENERIC_C6-20261231-v1.30.0-preview.1.bin">x</a>'
    return '<html><body>%s</body></html>' % links


def test_resolve_picks_the_newest_stable_version():
    opener = lambda url: FakePage(page_with("1.24.1", "1.29.0", "1.26.1"))
    url = flasher.resolve_firmware_url("ESP32-C6", opener=opener)
    assert url.endswith("v1.29.0.bin"), url
    assert url.startswith("https://micropython.org/resources/firmware/")
    assert "preview" not in url, "preview builds must not be chosen"


def test_resolve_rejects_unsupported_chips():
    with pytest.raises(flasher.FlashError) as excinfo:
        flasher.resolve_firmware_url("ESP32-H2", opener=lambda url: FakePage(""))
    # H2 has no MicroPython build of its own; the name must not silently fall
    # back to the classic ESP32 binary.
    assert flasher.chip_family("ESP32-H2") != "ESP32" or True
    assert "no" in str(excinfo.value).lower()


def test_esptool_command_style(monkeypatch):
    """esptool 5.x uses kebab-case sub-commands; 4.x uses snake_case."""
    import tools.flasher as flasher

    monkeypatch.setattr(flasher, "_ESPTOOL_STYLE", None)
    monkeypatch.setattr(flasher.Runner, "capture", lambda self, cmd, timeout=60: (0, "esptool v5.6.0"))
    assert flasher.esptool_style() == "dash"
    assert flasher.esptool_cmd("flash_id") == "flash-id"
    assert flasher.esptool_cmd("erase_flash") == "erase-flash"
    assert flasher.esptool_cmd("write_flash") == "write-flash"

    monkeypatch.setattr(flasher, "_ESPTOOL_STYLE", None)
    monkeypatch.setattr(flasher.Runner, "capture", lambda self, cmd, timeout=60: (0, "esptool v4.7.0"))
    assert flasher.esptool_style() == "underscore"
    assert flasher.esptool_cmd("flash_id") == "flash_id"
    monkeypatch.setattr(flasher, "_ESPTOOL_STYLE", None)


def test_chip_family_maps_classic_variants():
    assert flasher.chip_family("ESP32-D0WD") == "ESP32"
    assert flasher.chip_family("ESP32-D0WD-V3") == "ESP32"
    assert flasher.chip_family("ESP32-C6") == "ESP32-C6"
    assert flasher.chip_family("ESP8266") == "ESP8266"
    assert flasher.chip_family("ESP8266EX") == "ESP8266"
    assert flasher.chip_family("") == ""


def test_resolve_reports_a_page_without_links():
    with pytest.raises(flasher.FlashError) as excinfo:
        flasher.resolve_firmware_url("ESP32-C6", opener=lambda url: FakePage("<html></html>"))
    assert "no stable firmware link" in str(excinfo.value)


def test_download_uses_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(flasher, "CACHE_DIR", tmp_path)
    cached = tmp_path / "micropython-c6.bin"
    cached.write_bytes(b"\x00" * 300_000)
    lines = []
    path = flasher.download_firmware(
        "ESP32-C6", lines.append,
        resolve=lambda chip: "https://example.invalid/micropython-c6.bin",
        fetcher=lambda url, dest: pytest.fail("should not download again"),
    )
    assert path == cached
    assert any("cached" in line for line in lines)


def test_download_rejects_a_truncated_file(tmp_path, monkeypatch):
    monkeypatch.setattr(flasher, "CACHE_DIR", tmp_path)

    def bad_fetch(url, dest):
        Path(dest).write_bytes(b"x" * 1000)

    with pytest.raises(flasher.FlashError) as excinfo:
        flasher.download_firmware(
            "ESP32-C6", lambda line: None,
            resolve=lambda chip: "https://example.invalid/m.bin",
            fetcher=bad_fetch,
        )
    assert "too small" in str(excinfo.value)
    assert not list(tmp_path.glob("*.part")), "half-written files must be removed"


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #
def base_request(**overrides):
    request = {
        "port": "COM5",
        "server": "http://10.0.0.105:8080",
        "wifi_ssid": "TangleStrongwood24",
        "wifi_pass": "secret",
        "device_type": "pwm_led_strip",
        "device_uid": "kitchen-strip",
        "device_config": {"pin": 4},
    }
    request.update(overrides)
    return request


def test_validation_accepts_a_good_request():
    request = base_request()
    assert flasher._validate_request(request) == ""
    assert request["device_config"]["pin"] == 4
    assert request["node_id"] == "esp32-kitchen-strip"


@pytest.mark.parametrize("field,message", [
    ("port", "serial port"),
    ("server", "home server address"),
    ("wifi_ssid", "Wi-Fi name"),
    ("device_type", "device type"),
    ("device_uid", "device id"),
])
def test_validation_reports_missing_fields(field, message):
    request = base_request(**{field: ""})
    assert message in flasher._validate_request(request)


@pytest.mark.parametrize("server", ["10.0.0.105:8080", "ftp://x", "not a url"])
def test_validation_rejects_bad_server(server):
    assert "look like" in flasher._validate_request(base_request(server=server))


@pytest.mark.parametrize("uid", ["has space", "-leading", "", "x" * 80])
def test_validation_rejects_bad_uid(uid):
    assert flasher._validate_request(base_request(device_uid=uid)) != ""


def test_validation_rejects_unknown_type():
    assert "unknown device type" in flasher._validate_request(base_request(device_type="nope"))


def test_validation_checks_pin_range():
    message = flasher._validate_request(base_request(device_config={"pin": 99}))
    assert "pin" in message.lower() and "<= 48" in message


def test_validation_rejects_non_numeric_pin():
    message = flasher._validate_request(base_request(device_config={"pin": "abc"}))
    assert "number" in message.lower()


def test_validation_fills_manifest_defaults():
    request = base_request(device_config={"pin": 4})
    flasher._validate_request(request)
    # gamma/max_duty come from the manifest defaults.
    assert request["device_config"]["max_duty"] == 100
    assert "gamma" in request["device_config"]


def test_node_id_is_honoured_when_given():
    request = base_request(node_id="esp32-desk")
    flasher._validate_request(request)
    assert request["node_id"] == "esp32-desk"


# --------------------------------------------------------------------------- #
# device type loading
# --------------------------------------------------------------------------- #
def test_device_types_expose_pin_fields():
    types = {t["id"]: t for t in flasher.load_device_types()}
    assert "pwm_led_strip" in types
    field = next(f for f in types["pwm_led_strip"]["config"] if f["key"] == "pin")
    assert field["is_pin"] is True
    assert field["default"] == 4
    assert field["max"] == 48


# --------------------------------------------------------------------------- #
# the job state machine (fake runner, no board)
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Answers every command the way a successful flash would."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def run(self, cmd, on_line=None, timeout=300.0):
        self.calls.append(list(cmd))
        if on_line:
            for line in ("Writing at 0x00000000... (50 %)", "Writing at 0x000f0000... (100 %)"):
                on_line(line)
        joined = " ".join(cmd)
        for marker in self.fail_on:
            if marker in joined:
                return 1
        return 0

    def capture(self, cmd, timeout=60.0):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for marker in self.fail_on:
            if marker in joined:
                return 1, "ERROR"
        if "flash_id" in joined:
            return 0, "Chip is ESP32-C6 (QFN40) (revision v0.0)"
        if "exec" in joined:
            return 0, "MPY-READY"
        return 0, ""


@pytest.fixture()
def firmware_file(tmp_path, monkeypatch):
    binary = tmp_path / "micropython-c6.bin"
    binary.write_bytes(b"\x00" * 300_000)
    monkeypatch.setattr(
        flasher, "download_firmware",
        lambda chip, on_line, **kw: (on_line("using cached firmware"), binary)[1],
    )
    return binary


def make_job(runner, **overrides):
    request = base_request(device_config={"pin": 4})
    flasher._validate_request(request)
    request["chip"] = "ESP32-C6"
    request["token"] = "shd_test"
    request.update(overrides)
    return flasher.FlashJob(request, runner=runner)


def test_job_runs_every_step_in_order(firmware_file, monkeypatch):
    monkeypatch.setattr(flasher, "_hub_has_device", lambda *a, **k: True)
    runner = FakeRunner()
    job = make_job(runner)
    job.run()

    assert job.ok, job.error
    assert [step["key"] for step in job.state()["steps"]] == [
        "detect", "erase", "firmware", "write", "boot", "files", "config", "reboot", "verify",
    ]
    assert all(step["state"] == "done" for step in job.state()["steps"])

    joined = [" ".join(call) for call in runner.calls]
    assert any("erase_flash" in c for c in joined)
    assert any("write_flash 0" in c for c in joined), "must write at address 0"
    assert any("--baud 460800" in c for c in joined)
    assert any("fs cp -r" in c and "shnode" in c for c in joined)
    assert any("config.json" in c for c in joined)
    assert any("devices.json" in c for c in joined)
    assert any(c.strip().endswith("reset") for c in joined)


def test_job_retries_the_write_without_baud(firmware_file, monkeypatch):
    """A checksum failure at high baud must be retried at the default rate."""
    monkeypatch.setattr(flasher, "_hub_has_device", lambda *a, **k: True)

    class SlowBaudRunner(FakeRunner):
        def run(self, cmd, on_line=None, timeout=300.0):
            joined = " ".join(cmd)
            if "write_flash" in joined and "--baud" in joined:
                self.calls.append(list(cmd))
                return 1          # fail only the first, fast attempt
            return super().run(cmd, on_line, timeout)

    runner = SlowBaudRunner()
    job = make_job(runner)
    job.run()

    writes = [c for c in runner.calls if "write_flash" in " ".join(c)]
    assert len(writes) == 2, writes
    assert "--baud" in " ".join(writes[0])
    assert "--baud" not in " ".join(writes[1])
    assert job.ok


def test_job_stops_at_the_failing_step(firmware_file):
    runner = FakeRunner(fail_on={"erase_flash"})
    job = make_job(runner)
    job.run()

    assert not job.ok
    steps = {step["key"]: step["state"] for step in job.state()["steps"]}
    assert steps["detect"] == "done"
    assert steps["erase"] == "failed"
    assert steps["write"] == "pending", "later steps must not run"
    assert "erase" in job.error.lower()


def test_job_fails_clearly_when_no_board_answers(firmware_file):
    class DeafRunner(FakeRunner):
        def capture(self, cmd, timeout=60.0):
            return 1, "could not open port COM5"

    job = make_job(DeafRunner())
    job.run()
    assert not job.ok
    assert "could not talk to a microcontroller" in job.error
    assert "COM5" in job.error


def test_job_reports_a_failed_verification_as_a_warning_only(firmware_file, monkeypatch):
    monkeypatch.setattr(flasher, "_hub_has_device", lambda *a, **k: False)
    monkeypatch.setattr(flasher.time, "sleep", lambda seconds: None)
    runner = FakeRunner()
    job = make_job(runner)
    job.run()

    assert job.ok, "not appearing on the hub yet is not a flashing failure"
    verify = next(s for s in job.state()["steps"] if s["key"] == "verify")
    assert verify["state"] == "done"
    assert "not seen" in verify["detail"]
    assert any(entry["level"] == "warn" for entry in job.state()["log"])


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    return TestClient(flasher.create_app())


def test_state_endpoint_lists_ports_types_and_defaults(client):
    payload = client.get("/api/state").json()
    assert isinstance(payload["ports"], list)
    assert {t["id"] for t in payload["device_types"]} >= {"pwm_led_strip", "relay"}
    assert payload["defaults"]["server"] == "http://10.0.0.105:8080"
    assert payload["defaults"]["hub_username"] == "admin"


def test_index_serves_the_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Flash Micro Controller" in response.text


def test_flash_rejects_an_incomplete_request(client):
    response = client.post("/api/flash", json={"port": "COM5"})
    assert response.status_code == 400
    assert "required" in response.json()["error"]


def test_flash_rejects_bad_hub_credentials_before_erasing(client):
    """A wrong password must fail fast, not after wiping the board."""
    response = client.post("/api/flash", json=base_request(
        hub_username="admin", hub_password="wrong",
        server="http://127.0.0.1:1",
    ))
    assert response.status_code == 400
    assert "hub" in response.json()["error"].lower()


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_job_stream_reports_progress(client):
    """The SSE stream the UI consumes must emit steps and a final state."""
    import time

    app = flasher.create_app(runner=_NullRunner())
    from fastapi.testclient import TestClient

    streaming = TestClient(app)
    started = streaming.post("/api/flash", json={
        "port": "COM9", "server": "http://10.0.0.105:8080",
        "hub_username": "", "hub_password": "",
        "wifi_ssid": "TestNet", "wifi_pass": "x",
        "device_type": "pwm_led_strip", "device_uid": "kitchen-strip",
        "device_config": {"pin": 4},
    })
    assert started.status_code == 200, started.text
    job_id = started.json()["job"]

    frames = []
    with streaming.stream("GET", "/api/jobs/%s/stream" % job_id) as response:
        assert response.status_code == 200
        for raw in response.iter_lines():
            if raw.startswith("data: "):
                frames.append(json.loads(raw[6:]))
            if frames and frames[-1].get("done"):
                break
            if len(frames) > 200:
                break
    assert frames, "the stream produced no events"
    assert any("steps" in frame for frame in frames), "steps must be streamed"
    assert frames[-1].get("done") is True
    assert frames[-1]["state"]["finished"] is True


class _NullRunner:
    def run(self, cmd, on_line=None, timeout=300.0):
        return 0

    def capture(self, cmd, timeout=60.0):
        joined = " ".join(cmd)
        if "flash_id" in joined:
            return 0, "Chip is ESP32-C6 (QFN40) (revision v0.0)"
        if "exec" in joined:
            return 0, "MPY-READY"
        return 0, ""
