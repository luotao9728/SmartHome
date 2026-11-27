"""SmartHome flasher - a local web UI for provisioning ESP32 boards.

The board is plugged into *this* machine over USB, so this small service runs
here (not on the Pi) and talks to the serial port directly.  Open the page,
pick the detected port, fill in the form and press Flash.

    python tools/flasher.py
    # then open http://127.0.0.1:8090

What it does, in order, streaming progress to the browser:

    detect -> erase -> download MicroPython -> write firmware
           -> wait for the REPL -> copy the SmartHome firmware
           -> write /config.json and /devices.json -> reboot

It needs ``esptool``, ``mpremote`` and ``pyserial``:

    python -m pip install esptool mpremote pyserial
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = ROOT / "micropython"
DEVICE_TYPES_DIR = ROOT / "device_types"
UI_FILE = Path(__file__).resolve().parent / "flasher_ui.html"

DEFAULT_HUB = "http://10.0.0.105:8080"
DEFAULT_BAUD = 460800
CACHE_DIR = Path(tempfile.gettempdir()) / "smarthome-firmware"

#: esptool's chip name -> the MicroPython download page for that family.
CHIP_FAMILIES = {
    "ESP32-C6": "ESP32_GENERIC_C6",
    "ESP32-C3": "ESP32_GENERIC_C3",
    "ESP32-C2": "ESP32_GENERIC_C2",
    "ESP32-S3": "ESP32_GENERIC_S3",
    "ESP32-S2": "ESP32_GENERIC_S2",
    "ESP32": "ESP32_GENERIC",
    "ESP8266": "ESP8266_GENERIC",
}

#: Boards that are not flashed over a serial bootloader at all: they appear as a
#: USB mass-storage drive in BOOTSEL mode and take a .uf2 file.  "port" is a
#: drive path (``R:\`` on Windows) rather than a COM port.
UF2_BOARDS = {
    "RP2": {
        "page": "RPI_PICO2_W",
        "name": "Raspberry Pi Pico 2 W",
        # MicroPython names the RP2350 Pico 2 W image RPI_PICO2_W.
        "volume_labels": ("RP2350", "RPI-RP2", "PICO2W", "RPI-RP2W"),
    },
}

#: Marker file every RP2 BOOTSEL volume contains.
UF2_INFO_FILE = "INFO_UF2.TXT"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SmartHome-flasher/1.0"


class FlashError(RuntimeError):
    """A step failed; the message is shown in the UI."""


#: esptool 5.x renamed every sub-command from snake_case to kebab-case
#: (flash_id -> flash-id, erase_flash -> erase-flash, ...).  Detect once and use
#: the spelling the installed version understands rather than pinning one.
_ESPTOOL_STYLE: Optional[str] = None


def esptool_style(runner: Optional["Runner"] = None) -> str:
    global _ESPTOOL_STYLE
    if _ESPTOOL_STYLE is not None:
        return _ESPTOOL_STYLE
    if os.environ.get("SMARTHOME_ESPTOOL_STYLE") in ("dash", "underscore"):
        _ESPTOOL_STYLE = os.environ["SMARTHOME_ESPTOOL_STYLE"]
        return _ESPTOOL_STYLE
    runner = runner or Runner()
    code, output = runner.capture([sys.executable, "-m", "esptool", "version"], timeout=30)
    _ESPTOOL_STYLE = "dash" if code == 0 and re.search(r"\bv?5\.", output) else "underscore"
    return _ESPTOOL_STYLE


def esptool_cmd(name: str, runner: Optional["Runner"] = None) -> str:
    """Return the sub-command name in the installed esptool's convention."""
    if esptool_style(runner) == "dash":
        return name.replace("_", "-")
    return name


# --------------------------------------------------------------------------- #
# command execution (injectable so the tests do not need a board)
# --------------------------------------------------------------------------- #
class Runner:
    """Runs a subprocess and streams its output line by line."""

    def run(self, cmd: List[str], on_line: Optional[Callable[[str], None]] = None,
            timeout: float = 300.0) -> int:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            errors="replace",
        )
        started = time.monotonic()
        buffer = ""
        assert proc.stdout is not None
        try:
            while True:
                char = proc.stdout.read(1)
                if not char:
                    break
                if char in "\r\n":
                    line = buffer.strip()
                    buffer = ""
                    if line and on_line is not None:
                        on_line(line)
                else:
                    buffer += char
                if time.monotonic() - started > timeout:
                    proc.kill()
                    raise FlashError("timed out after %.0fs: %s" % (timeout, " ".join(cmd)))
            if buffer.strip() and on_line is not None:
                on_line(buffer.strip())
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
        return proc.wait(timeout=30)

    def capture(self, cmd: List[str], timeout: float = 60.0) -> "tuple[int, str]":
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# --------------------------------------------------------------------------- #
# board discovery
# --------------------------------------------------------------------------- #
def find_uf2_volumes() -> List[Dict[str, Any]]:
    """BOOTSEL mass-storage volumes (a Pico 2 W waiting for a .uf2).

    In BOOTSEL mode these boards have **no serial port** — they show up as a
    removable drive containing INFO_UF2.TXT.  Returns one entry per volume.
    """
    volumes: List[Dict[str, Any]] = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            root = Path("%s:\\" % letter)
            try:
                if not root.exists():
                    continue
                info = root / UF2_INFO_FILE
                if not info.is_file():
                    continue
            except OSError:
                continue
            label = ""
            model = ""
            try:
                text = info.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if line.lower().startswith("model:"):
                        model = line.split(":", 1)[1].strip()
                    elif line.lower().startswith("board-id:"):
                        label = line.split(":", 1)[1].strip()
            except OSError:
                pass
            combined = ("%s %s" % (label, model)).upper()
            is_rp2 = any(token in combined for token in ("RP2", "RP2350", "RPI-RP2",
                                                         "PICO", "RASPBERRY PI"))
            volumes.append({
                "device": str(root),
                "description": model or label or "UF2 bootloader volume",
                "vendor": "Raspberry Pi",
                "family": "RP2" if is_rp2 else "",
                "likely_microcontroller": is_rp2,
                "kind": "uf2",
            })
        return volumes

    # Linux / macOS: a mounted FAT volume with INFO_UF2.TXT at its root.
    candidates = [Path("/media"), Path("/run/media"), Path("/Volumes")]
    for base in candidates:
        if not base.is_dir():
            continue
        try:
            for mount in base.iterdir():
                info = mount / UF2_INFO_FILE
                if info.is_file():
                    volumes.append({
                        "device": str(mount),
                        "description": "UF2 bootloader volume",
                        "vendor": "Raspberry Pi",
                        "family": "RP2",
                        "likely_microcontroller": True,
                        "kind": "uf2",
                    })
        except OSError:
            continue
    return volumes


def list_ports() -> List[Dict[str, Any]]:
    """Serial ports plus UF2 volumes, with a hint about which look like a board."""
    try:
        from serial.tools import list_ports as serial_list_ports
    except ImportError:
        serial_list_ports = None

    interesting = {"303A": "Espressif", "2E8A": "Raspberry Pi", "1A86": "CH34x",
                   "10C4": "CP210x", "0403": "FTDI", "2341": "Arduino"}
    ports: List[Dict[str, Any]] = []
    for port in (serial_list_ports.comports() if serial_list_ports else []):
        hwid = (port.hwid or "").upper()
        label = ""
        for vid, name in interesting.items():
            if "VID:PID=%s" % vid in hwid or "VID_%s" % vid in hwid:
                label = name
                break
        # An RP2 running MicroPython shows up as 2E8A over USB CDC.
        family = "RP2" if "2E8A" in hwid else ("ESP32" if "303A" in hwid else "")
        ports.append({
            "device": port.device,
            "description": port.description or "",
            "hwid": port.hwid or "",
            "vendor": label,
            "family": family,
            "likely_microcontroller": bool(label),
            "kind": "serial",
        })

    try:
        ports.extend(find_uf2_volumes())
    except Exception:  # noqa: BLE001 - a missing drive must not hide the ports
        pass

    ports.sort(key=lambda p: (not p["likely_microcontroller"], p["device"]))
    return ports


def board_kind(device: str) -> str:
    """``uf2`` for a BOOTSEL volume, otherwise ``serial``."""
    path = Path(str(device))
    try:
        if path.is_dir() or (os.name == "nt" and len(str(device)) <= 3):
            if (path / UF2_INFO_FILE).is_file():
                return "uf2"
            if os.name == "nt" and path.is_dir():
                return "uf2"
    except OSError:
        pass
    return "serial"


def find_port_like(device: str) -> Optional[str]:
    """Re-find a board after it re-enumerates with a different COM number."""
    ports = list_ports()
    wanted = None
    for port in ports:
        if port["device"] == device:
            wanted = port["hwid"]
            break
    if wanted:
        for port in ports:
            if port["hwid"] == wanted:
                return port["device"]
    candidates = [p["device"] for p in ports if p["likely_microcontroller"]]
    return candidates[0] if candidates else (ports[0]["device"] if ports else None)


def find_serial_port_like(device: str) -> Optional[str]:
    """Same, but only serial ports — never a mounted UF2 volume.

    ``mpremote connect`` cannot open a drive path, and after a UF2 write the old
    handle is gone while the board reappears under a new name.
    """
    ports = [p for p in list_ports() if p.get("kind", "serial") == "serial"]
    wanted = None
    for port in ports:
        if port["device"] == device:
            wanted = port["hwid"]
            break
    if wanted:
        for port in ports:
            if port["hwid"] == wanted:
                return port["device"]
    # Prefer a Raspberry Pi (2E8A) port after a Pico flash, then anything
    # that looks like a board.
    for port in ports:
        if port.get("family") == "RP2":
            return port["device"]
    candidates = [p["device"] for p in ports if p["likely_microcontroller"]]
    if candidates:
        return candidates[0]
    return ports[0]["device"] if ports else None


#: Chip names as printed by esptool, across major versions:
#:   4.x: "Chip is ESP32-C6 (QFN40) (revision v0.0)"
#:   5.x: "Detecting chip type... ESP32-C6"
#: Both are matched; a bare chip name elsewhere in the output is the fallback.
CHIP_RE = re.compile(
    r"(?:Chip is|Detecting chip type\.{0,3}\s*)(ESP8266[A-Z]*|ESP32(?:-[A-Z0-9]+)*)"
)
CHIP_FALLBACK_RE = re.compile(r"\b(ESP8266[A-Z]*|ESP32(?:-[A-Z0-9]+)*)\b")

#: Classic ESP32 variants (ESP32-D0WD, ESP32-PICO, ...) all use the same build.
#: Chip names esptool reports that share one MicroPython build family.
CHIP_ALIASES = {
    "ESP32-D0WD": "ESP32", "ESP32-D0WDQ6": "ESP32", "ESP32-D0WD-V3": "ESP32",
    "ESP32-PICO-D4": "ESP32", "ESP32-PICO-V3": "ESP32", "ESP32-S0WD": "ESP32",
}


def chip_family(chip: str) -> str:
    """Reduce an esptool chip name to the MicroPython build family."""
    if not chip:
        return ""
    if chip in CHIP_FAMILIES:
        return chip
    if chip in CHIP_ALIASES:
        return CHIP_ALIASES[chip]
    # "ESP8266EX" is what the ROM reports for the ESP8266 family.
    if chip.startswith("ESP8266"):
        return "ESP8266"
    for name in sorted(CHIP_FAMILIES, key=len, reverse=True):
        if chip.startswith(name):
            return name
    return chip
PERCENT_RE = re.compile(r"\((\d{1,3})\s*%")


def parse_chip(text: str) -> Optional[str]:
    if not text:
        return None
    match = CHIP_RE.search(text)
    if match:
        return match.group(1)
    # Stub-flasher banners and older builds may only print the bare name.
    match = CHIP_FALLBACK_RE.search(text)
    return match.group(1) if match else None


def parse_percent(text: str) -> Optional[int]:
    match = PERCENT_RE.search(text)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# firmware download
# --------------------------------------------------------------------------- #
def firmware_page_url(chip: str) -> Optional[str]:
    family = CHIP_FAMILIES.get(chip_family(chip))
    if not family:
        return None
    return "https://micropython.org/download/%s/" % family


def resolve_firmware_url(chip: str, *, opener=None) -> str:
    """Newest stable MicroPython .bin for this chip, scraped from micropython.org."""
    page = firmware_page_url(chip)
    if page is None:
        raise FlashError(
            "no known MicroPython build for chip %r (supported: %s)"
            % (chip, ", ".join(sorted(CHIP_FAMILIES)))
        )
    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30))
    try:
        html = opener(page).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise FlashError("could not reach micropython.org: %s" % exc)
    pattern = re.compile(
        r'href="(/resources/firmware/[^"]+-v(\d+\.\d+\.\d+)\.bin)"')
    found = []
    for path, version in pattern.findall(html):
        found.append((tuple(int(part) for part in version.split(".")), path))
    if not found:
        raise FlashError("no stable firmware link found on %s" % page)
    found.sort()
    return "https://micropython.org" + found[-1][1]


def _http_download(url: str, dest: str) -> None:
    """Download ``url`` to ``dest``.

    ``urllib.request.urlretrieve`` only accepts a string URL: passing a Request
    object makes it call ``url.split()`` on the object, which raises
    "expected string or bytes-like object".  So the request is made explicitly
    and the body streamed to disk.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        expected = response.headers.get("Content-Length")
        written = 0
        with open(dest, "wb") as handle:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
        if expected is not None:
            try:
                declared = int(expected)
            except (TypeError, ValueError):
                declared = None
            if declared is not None and written != declared:
                raise FlashError(
                    "download was truncated (%d of %d bytes)" % (written, declared)
                )


def resolve_uf2_url(board: str, *, opener=None) -> str:
    """Newest stable .uf2 for an RP2 board (Pico 2 W), scraped from micropython.org."""
    spec = UF2_BOARDS.get(board)
    if spec is None:
        raise FlashError(
            "no known UF2 build for board %r (supported: %s)"
            % (board, ", ".join(sorted(UF2_BOARDS)))
        )
    page = "https://micropython.org/download/%s/" % spec["page"]
    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30))
    try:
        html = opener(page).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise FlashError("could not reach micropython.org: %s" % exc)
    pattern = re.compile(r'href="(/resources/firmware/[^"]+-v(\d+\.\d+\.\d+)\.uf2)"')
    found = []
    for path, version in pattern.findall(html):
        # The Pico 2 W page also offers a -RISCV variant; the default (ARM)
        # image is the one that behaves like every other MicroPython port.
        if "-RISCV" in path.upper():
            continue
        found.append((tuple(int(part) for part in version.split(".")), path))
    if not found:
        raise FlashError(
            "no stable .uf2 found on %s — open that page in a browser and check"
            % page
        )
    found.sort()
    return "https://micropython.org" + found[-1][1]


def copy_to_uf2_volume(source: Path, volume: Path, on_line=None) -> Path:
    """Copy a .uf2 onto a BOOTSEL volume; the bootloader reboots on completion."""
    target = volume / source.name
    on_line = on_line or (lambda line: None)
    on_line("copying %s to %s" % (source.name, volume))
    size = source.stat().st_size
    written = 0
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    if written != size:
        raise FlashError(
            "the copy was incomplete (%d of %d bytes) — the drive may have "
            "disconnected early" % (written, size)
        )
    on_line("wrote %.1f MB" % (written / 1e6))
    return target


def download_uf2(board: str, on_line, *, resolve=None, fetcher=None) -> Path:
    return download_firmware(
        board, on_line,
        resolve=lambda name: resolve_uf2_url(name),
        fetcher=fetcher,
    )


def download_firmware(chip: str, on_line, *, resolve=None, fetcher=None) -> Path:
    resolve = resolve or resolve_firmware_url
    url = resolve(chip)
    name = url.rsplit("/", 1)[-1]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / name
    if target.is_file() and target.stat().st_size > 200_000:
        on_line("using cached %s (%.1f MB)" % (name, target.stat().st_size / 1e6))
        return target
    on_line("downloading %s" % url)
    fetcher = fetcher or _http_download
    partial = target.with_suffix(".part")
    try:
        fetcher(url, str(partial))
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        raise FlashError("firmware download failed: %s" % exc)
    if not partial.is_file() or partial.stat().st_size < 200_000:
        partial.unlink(missing_ok=True)
        raise FlashError("downloaded file looks wrong (too small)")
    os.replace(partial, target)
    on_line("saved %s (%.1f MB)" % (name, target.stat().st_size / 1e6))
    return target


# --------------------------------------------------------------------------- #
# device types
# --------------------------------------------------------------------------- #
def load_device_types() -> List[Dict[str, Any]]:
    """Manifests, reduced to what the form needs."""
    types = []
    if not DEVICE_TYPES_DIR.is_dir():
        return types
    for path in sorted(DEVICE_TYPES_DIR.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or not manifest.get("id"):
            continue
        config_fields = []
        for key, spec in (manifest.get("config") or {}).items():
            if not isinstance(spec, dict):
                continue
            config_fields.append({
                "key": key,
                "label": spec.get("label") or key.replace("_", " ").title(),
                "type": spec.get("type", "int"),
                "default": spec.get("default"),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "unit": spec.get("unit", ""),
                "is_pin": key.startswith("pin"),
            })
        types.append({
            "id": manifest["id"],
            "name": manifest.get("name", manifest["id"]),
            "description": manifest.get("description", ""),
            "config": config_fields,
        })
    types.sort(key=lambda t: t["name"])
    return types


def validate_config(type_spec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce and range-check the hardware config the user typed."""
    clean: Dict[str, Any] = {}
    for spec in type_spec.get("config", []):
        key = spec["key"]
        raw = values.get(key, spec.get("default"))
        kind = spec.get("type", "int")
        if raw is None or raw == "":
            if spec.get("default") is not None:
                clean[key] = spec["default"]
            continue
        if kind == "bool":
            clean[key] = bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
        elif kind in ("int", "float"):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                raise FlashError("%s must be a number" % spec["label"])
            if kind == "int":
                if number != int(number):
                    raise FlashError("%s must be a whole number" % spec["label"])
                number = int(number)
            low, high = spec.get("min"), spec.get("max")
            if low is not None and number < low:
                raise FlashError("%s must be >= %s" % (spec["label"], low))
            if high is not None and number > high:
                raise FlashError("%s must be <= %s" % (spec["label"], high))
            clean[key] = number
        else:
            clean[key] = str(raw)
    return clean


# --------------------------------------------------------------------------- #
# flashing
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    key: str
    label: str
    state: str = "pending"      # pending | running | done | failed
    detail: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "state": self.state,
            "detail": self.detail,
            "seconds": (round((self.finished or time.time()) - self.started, 1)
                        if self.started else None),
        }


#: The ESP32 route: a serial bootloader that esptool drives.
STEPS = [
    ("detect", "Detect board"),
    ("erase", "Erase flash"),
    ("firmware", "Get MicroPython"),
    ("write", "Write MicroPython"),
    ("boot", "Wait for REPL"),
    ("files", "Copy SmartHome firmware"),
    ("config", "Write config.json and devices.json"),
    ("reboot", "Reboot"),
    ("verify", "Verify on the hub"),
]

#: The RP2 route: BOOTSEL is a USB drive, so "erase" does not apply.
UF2_STEPS = [
    ("detect", "Find BOOTSEL drive"),
    ("firmware", "Get MicroPython"),
    ("write", "Write MicroPython"),
    ("boot", "Wait for REPL"),
    ("files", "Copy SmartHome firmware"),
    ("config", "Write config.json and devices.json"),
    ("reboot", "Reboot"),
    ("verify", "Verify on the hub"),
]


class FlashJob:
    def __init__(self, request: Dict[str, Any], runner: Optional[Runner] = None):
        self.id = uuid.uuid4().hex[:12]
        self.request = request
        self.runner = runner or Runner()
        self.created = time.time()
        self.finished: Optional[float] = None
        self.error: str = ""
        self.ok = False
        # A BOOTSEL volume means the RP2 path: no esptool, no erase step.
        self.kind = "uf2" if request.get("port_kind") == "uf2" else "serial"
        steps = UF2_STEPS if self.kind == "uf2" else STEPS
        self.steps = {key: Step(key, label) for key, label in steps}
        self.order = [key for key, _ in steps]
        self.log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- event log ------------------------------------------------------- #
    def emit(self, level: str, text: str, step: Optional[str] = None) -> None:
        with self._lock:
            self.log.append({
                "ts": time.time(), "level": level, "text": text, "step": step,
            })
            del self.log[:-400]

    def _begin(self, key: str, detail: str = "") -> None:
        step = self.steps[key]
        step.state = "running"
        step.started = time.time()
        step.detail = detail
        self.emit("info", step.label + (" - " + detail if detail else ""), key)

    def _done(self, key: str, detail: str = "") -> None:
        step = self.steps[key]
        step.state = "done"
        step.finished = time.time()
        if detail:
            step.detail = detail

    def _fail(self, key: str, message: str) -> None:
        step = self.steps[key]
        step.state = "failed"
        step.finished = time.time()
        step.detail = message
        self.error = message
        self.emit("error", message, key)
        self.finished = time.time()

    # -- helpers --------------------------------------------------------- #
    def _esptool(self) -> List[str]:
        return [sys.executable, "-m", "esptool"]

    def _mpremote(self) -> List[str]:
        return [sys.executable, "-m", "mpremote"]

    def _run(self, cmd: List[str], key: str, *, timeout: float = 300.0,
             percent: bool = False) -> int:
        def on_line(line: str) -> None:
            if percent:
                value = parse_percent(line)
                if value is not None:
                    self.steps[key].detail = "%d%%" % value
            self.emit("cmd", line, key)
        return self.runner.run(cmd, on_line, timeout=timeout)

    # -- the job --------------------------------------------------------- #
    def run(self) -> None:
        req = self.request
        self.emit("info", "starting %s flash for %s on %s"
                  % ("UF2" if self.kind == "uf2" else "serial",
                     req["device_uid"], req["port"]))
        try:
            if self.kind == "uf2":
                self._step_detect_uf2(req)
            else:
                self._step_detect(req)
                self._step_erase(req)
            binary = self._step_firmware(req)
            port = self._step_write(req, binary)
            port = self._step_boot(port)
            self._step_files(port)
            self._step_config(port)
            self._step_reboot(port)
            self._step_verify(req)
            self.ok = True
            self.emit("ok", "done - %s should appear on the hub shortly" % req["device_uid"])
        except FlashError as exc:
            if not self.error:
                self._fail(self._current_step(), str(exc))
        except Exception as exc:  # noqa: BLE001 - always end the job cleanly
            self._fail(self._current_step(), "%s: %s" % (type(exc).__name__, exc))
        finally:
            self.finished = self.finished or time.time()

    def _current_step(self) -> str:
        for key in reversed(self.order):
            if self.steps[key].state in ("running", "failed"):
                return key
        return self.order[0]

    # -- steps ----------------------------------------------------------- #
    def _detect_chip(self, port: str) -> "tuple[Optional[str], str]":
        """Identify the chip on ``port``.

        The C6 and other native-USB parts have no DTR/RTS lines, so the default
        reset cannot work; ``--before usb-reset`` resets through the USB stack
        instead.  Boards with a USB-serial bridge ignore the option harmlessly,
        so it is always passed.
        """
        code, output = self.runner.capture(
            self._esptool() + ["--port", port, "--before", "usb-reset",
                               esptool_cmd("flash_id", self.runner)]
        )
        return parse_chip(output), output

    def _enter_bootloader(self, port: str) -> None:
        """Ask a board that is already running MicroPython to reboot into ROM.

        A board with MicroPython on it enumerates as a CDC device that esptool
        cannot drive, so flashing a *working* board used to require holding BOOT
        by hand -- impossible on a C6 devkit with no buttons.  ``mpremote
        bootloader`` is the supported way: it runs ``machine.bootloader()`` and
        waits for the port to drop.  The board then comes back as the ROM
        downloader, usually on a different COM number.
        """
        self.emit("info", "the board is running MicroPython; asking it to restart "
                          "into the bootloader", "detect")
        self.runner.capture(
            self._mpremote() + ["connect", port, "bootloader"],
            timeout=30,
        )
        # The CDC interface has to disappear before the ROM enumerates.
        time.sleep(1.0)

    def _wait_for_bootloader(self, old_port: str) -> Optional[str]:
        """Poll for a port esptool can identify, after a bootloader reboot.

        The board re-enumerates as a *different* USB device (the ROM downloader
        instead of the MicroPython CDC serial), usually on a new COM number, and
        that takes several seconds.  The old port is dropped from the candidate
        list after the first failed attempt so the remaining time is not spent
        re-probing a port that no longer exists.
        """
        deadline = time.monotonic() + 45
        tried_old = False
        seen = set()
        while time.monotonic() < deadline:
            time.sleep(1.5)
            candidates = []
            if old_port and not tried_old:
                candidates.append(old_port)
            for port in list_ports():
                if port.get("kind", "serial") != "serial":
                    continue
                if not port.get("likely_microcontroller"):
                    continue
                if port["device"] not in seen or port["device"] != old_port:
                    candidates.append(port["device"])
            # Keep the order stable but drop duplicates.
            ordered = []
            for device in candidates:
                if device not in ordered:
                    ordered.append(device)
            for candidate in ordered:
                chip, _ = self._detect_chip(candidate)
                if chip:
                    return candidate
                if candidate == old_port:
                    tried_old = True
                seen.add(candidate)
        return None

    def _step_detect(self, req: Dict[str, Any]) -> None:
        self._begin("detect", req["port"])
        chip, output = self._detect_chip(req["port"])

        if not chip:
            # Maybe MicroPython is already running and holding the port.
            self._enter_bootloader(req["port"])
            found = self._wait_for_bootloader(req["port"])
            if found:
                chip, output = self._detect_chip(found)
                if chip:
                    self.emit("info", "board restarted into the bootloader on %s"
                              % found, "detect")
                    req["port"] = found

        if not chip:
            # Keep the meaningful lines only: esptool mixes its banner, the real
            # error and a hexdump of the rom, and the tail is where the error is.
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            interesting = [line for line in lines
                           if "ERROR" in line.upper() or "Could not" in line
                           or "busy" in line.lower() or "Failed" in line
                           or "timeout" in line.lower()
                           or "A fatal error" in line]
            tail = " | ".join((interesting or lines)[-3:])
            hint = "the board did not restart into the bootloader"
            if "timeout" in output.lower():
                hint = ("the board stopped responding while restarting. Unplug "
                        "the USB cable for a few seconds and plug it back in")
            raise FlashError(
                "could not talk to a microcontroller on %s.%s (%s)"
                % (req["port"], " " + tail if tail else "", hint)
            )
        self._done("detect", chip)
        req["chip"] = chip

    def _step_detect_uf2(self, req: Dict[str, Any]) -> None:
        """Confirm the chosen path really is a BOOTSEL volume."""
        self._begin("detect", str(req["port"]))
        volume = Path(str(req["port"]))
        info = volume / UF2_INFO_FILE
        if not info.is_file():
            raise FlashError(
                "%s is not a BOOTSEL drive. Put the Pico 2 W into bootloader mode: "
                "hold BOOT, plug in USB, then release. It will appear as a "
                "removable drive containing INFO_UF2.TXT." % req["port"]
            )
        model = ""
        try:
            for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model:"):
                    model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        req["chip"] = "RP2"
        req["uf2_board"] = "RP2"
        self._done("detect", model or "RP2 bootloader")

    def _step_erase(self, req: Dict[str, Any]) -> None:
        self._begin("erase", "clearing the flash")
        code = self._run(
            self._esptool() + ["--port", req["port"], esptool_cmd("erase_flash", self.runner)],
            "erase", timeout=180.0,
        )
        if code != 0:
            raise FlashError("esptool could not erase the flash (exit %d)" % code)
        self._done("erase")

    def _step_firmware(self, req: Dict[str, Any]) -> Path:
        label = req.get("uf2_board") or req.get("chip", "")
        self._begin("firmware", label)
        emit = lambda line: self.emit("cmd", line, "firmware")
        if self.kind == "uf2":
            binary = download_uf2(label, emit)
        else:
            binary = download_firmware(label, emit)
        self._done("firmware", binary.name)
        return binary

    def _step_write(self, req: Dict[str, Any], binary: Path) -> str:
        self._begin("write", binary.name)
        if self.kind == "uf2":
            volume = Path(str(req["port"]))
            self.emit("info", "the board reboots by itself once the copy finishes",
                      "write")
            copy_to_uf2_volume(binary, volume,
                               lambda line: self.emit("cmd", line, "write"))
            self._done("write", binary.name)
            return str(volume)
        write = esptool_cmd("write_flash", self.runner)
        # --after hard-reset matters on native-USB chips: without it the board
        # stays in the ROM downloader after the write and MicroPython never
        # starts, so the boot wait below times out on a perfectly good flash.
        cmd = self._esptool() + [
            "--port", req["port"], "--baud", str(req.get("baud") or DEFAULT_BAUD),
            "--after", "hard-reset",
            write, "0", str(binary),
        ]
        code = self._run(cmd, "write", timeout=420.0, percent=True)
        if code != 0:
            # A slower link is the usual cause of a checksum failure.
            self.emit("warn", "write failed; retrying at the default baud rate", "write")
            cmd = self._esptool() + ["--port", req["port"], "--after", "hard-reset",
                                     write, "0", str(binary)]
            code = self._run(cmd, "write", timeout=600.0, percent=True)
        if code != 0:
            raise FlashError("esptool could not write the firmware (exit %d)" % code)
        self._done("write", binary.name)
        return req["port"]

    def _step_boot(self, port: str) -> str:
        """Wait for the MicroPython REPL and return its serial port.

        After a flash the board re-enumerates (a UF2 volume is replaced by a
        serial port, an ESP32 may change COM number), so the port is re-scanned
        every round.  Each mpremote attempt uses a short timeout: a long one
        burns the whole budget on a board that is still booting, which is why
        this used to give up after two tries.
        """
        self._begin("boot", "waiting for the MicroPython prompt")
        deadline = time.monotonic() + 90
        candidate = "" if self.kind == "uf2" else port
        while time.monotonic() < deadline:
            # Re-scan: the board re-enumerates under a new name after a flash.
            fresh = find_serial_port_like(port)
            if fresh and fresh != candidate:
                candidate = fresh
                self.emit("info", "board appeared on %s" % candidate, "boot")
            available = {p["device"] for p in list_ports()
                         if p.get("kind", "serial") == "serial"}
            if candidate and candidate in available:
                # Short timeout: a board that is still booting would otherwise
                # eat the whole budget in one call.  Never run esptool here --
                # on a native-USB board it blocks and cannot reset anything.
                code, output = self.runner.capture(
                    self._mpremote() + ["connect", candidate, "exec",
                                        "print('MPY-READY')"],
                    timeout=8,
                )
                if code == 0 and "MPY-READY" in output:
                    self._done("boot", "port " + candidate)
                    return candidate
            time.sleep(1.5)
        raise FlashError(
            "the firmware was written and verified, but MicroPython is not "
            "answering on %s. Unplug the USB cable, wait 5 seconds and plug it "
            "back in, then use \"Update Settings Only\" -- the board already has "
            "valid firmware." % port
        )

    def _step_files(self, port: str) -> None:
        self._begin("files", "boot.py, main.py, shnode/")
        self.emit("cmd", "copying the SmartHome firmware from %s" % FIRMWARE_DIR, "files")
        mpremote = self._mpremote()

        self.runner.run(mpremote + ["connect", port, "fs", "mkdir", ":shnode"],
                        None, timeout=60.0)
        code = self.runner.run(
            mpremote + ["connect", port, "fs", "cp", "-r", str(FIRMWARE_DIR / "shnode"), ":"],
            lambda line: self.emit("cmd", line, "files"), timeout=300.0,
        )
        if code != 0:
            raise FlashError("could not copy the shnode package (exit %d)" % code)
        for name in ("boot.py", "main.py"):
            code = self.runner.run(
                mpremote + ["connect", port, "fs", "cp", str(FIRMWARE_DIR / name), ":" + name],
                None, timeout=120.0,
            )
            if code != 0:
                raise FlashError("could not copy %s (exit %d)" % (name, code))
        self._done("files")

    def _step_config(self, port: str) -> None:
        self._begin("config", "network and device settings")
        staging = Path(tempfile.mkdtemp(prefix="shcfg-"))
        try:
            config = {
                "wifi_ssid": self.request.get("wifi_ssid", ""),
                "wifi_pass": self.request.get("wifi_pass", ""),
                "server": self.request.get("server", "").rstrip("/"),
                "node_id": self.request.get("node_id") or "",
                "token": self.request.get("token") or "",
                "heartbeat_s": 10,
                "report_s": 30,
            }
            devices = [{
                "type": self.request["device_type"],
                "uid": self.request["device_uid"],
                "name": self.request.get("device_name") or self.request["device_uid"],
                "config": self.request.get("device_config") or {},
            }]
            config_path = staging / "config.json"
            devices_path = staging / "devices.json"
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            devices_path.write_text(json.dumps(devices, indent=2), encoding="utf-8")
            self.emit("info", "token: %s" % ("yes" if config["token"] else "none (board will enrol)"),
                      "config")

            mpremote = self._mpremote()
            for local, remote in ((config_path, ":config.json"), (devices_path, ":devices.json")):
                code = self.runner.run(
                    mpremote + ["connect", port, "fs", "cp", str(local), remote],
                    None, timeout=120.0,
                )
                if code != 0:
                    raise FlashError("could not write %s (exit %d)" % (remote.lstrip(":"), code))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._done("config")

    def _step_reboot(self, port: str) -> None:
        self._begin("reboot")
        self.runner.run(self._mpremote() + ["connect", port, "reset"], None, timeout=60.0)
        self._done("reboot")

    def _step_verify(self, req: Dict[str, Any]) -> None:
        self._begin("verify", req.get("server", ""))
        deadline = time.monotonic() + 45
        uid = req["device_uid"]
        mismatch = ""
        while time.monotonic() < deadline:
            found = _hub_has_device(req.get("server", ""), uid, req.get("token", ""),
                                    req.get("hub_username", ""), req.get("hub_password", ""))
            if found:
                self._done("verify", "seen on the hub")
                return
            # Ask the board what network it actually joined: a board that is
            # "connected" but on another subnet can never reach the hub, and
            # that is worth saying explicitly rather than leaving the user to
            # guess from a timeout.
            if not mismatch:
                mismatch = self._network_mismatch(req)
                if mismatch:
                    self.emit("error", mismatch, "verify")
            time.sleep(3)
        # Not fatal: the board may still be joining Wi-Fi.
        step = self.steps["verify"]
        step.state = "done"
        step.finished = time.time()
        if mismatch:
            step.detail = "wrong network - see the message above"
            self.emit("warn", mismatch, "verify")
            return
        step.detail = "not seen yet - check the board's serial console"
        self.emit("warn", "the board did not appear on the hub within 45s; "
                          "check Wi-Fi credentials or the serial console", "verify")

    def _network_mismatch(self, req: Dict[str, Any]) -> str:
        """Compare the board's subnet with the hub's, if we can ask it."""
        try:
            code, output = self.runner.capture(
                self._mpremote() + [
                    "connect", str(req["port"]), "exec",
                    "import network;w=network.WLAN(network.STA_IF);"
                    "print('NET', w.isconnected(), w.ifconfig())",
                ],
                timeout=20,
            )
        except Exception:  # noqa: BLE001
            return ""
        if code != 0 or "NET" not in output:
            return ""
        match = re.search(r"NET\s+\w+\s+\(([^)]*)\)", output)
        if not match:
            return ""
        parts = [piece.strip().strip("'\"") for piece in match.group(1).split(",")]
        if not parts:
            return ""
        board_ip = parts[0]
        gateway = parts[2] if len(parts) > 2 else ""
        server = str(req.get("server") or "")
        host = re.sub(r"^https?://", "", server).split(":")[0].split("/")[0]
        if not board_ip or board_ip.startswith("0."):
            return ("the board has no IP address - it did not join Wi-Fi. Check "
                    "the SSID and password (and that the network is 2.4 GHz).")
        if host and board_ip.count(".") == 3 and host.count(".") == 3:
            board_net = ".".join(board_ip.split(".")[:3])
            hub_net = ".".join(host.split(".")[:3])
            if board_net != hub_net:
                return ("the board joined a different network: it is on %s "
                        "(gateway %s) but the hub is on %s. They must be on the "
                        "same subnet - check the Wi-Fi SSID, or that the board "
                        "is not stuck on its own access point."
                        % (board_ip, gateway or "unknown", hub_net + ".x"))
        return ""

    # -- state ----------------------------------------------------------- #
    def state(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "error": self.error,
            "finished": self.finished is not None,
            "created": self.created,
            "steps": [self.steps[key].public() for key in self.order],
            "log": list(self.log),
        }


def _hub_has_device(server: str, uid: str, token: str, username: str, password: str) -> bool:
    if not server:
        return False
    import urllib.error
    import urllib.request

    def get(url: str, headers: Dict[str, str]) -> Optional[str]:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None

    # Prefer the API key when we can log in; otherwise try without auth (the
    # hub answers 401 and we simply report "not seen").
    cookies: Dict[str, str] = {}
    if username and password:
        body = json.dumps({"username": username, "password": password}).encode()
        try:
            request = urllib.request.Request(
                server.rstrip("/") + "/api/login", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            with opener.open(request, timeout=8) as response:
                for header, value in response.headers.items():
                    if header.lower() == "set-cookie":
                        cookies["Cookie"] = value.split(";")[0]
        except Exception:  # noqa: BLE001
            pass
    payload = get(server.rstrip("/") + "/api/devices", cookies)
    if not payload:
        return False
    try:
        devices = json.loads(payload).get("devices", [])
    except ValueError:
        return False
    return any(d.get("uid") == uid for d in devices)


# --------------------------------------------------------------------------- #
# HTTP app
# --------------------------------------------------------------------------- #
def create_app(jobs: Optional[Dict[str, FlashJob]] = None, runner: Optional[Runner] = None):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

    registry: Dict[str, FlashJob] = jobs if jobs is not None else {}
    app = FastAPI(title="SmartHome flasher", docs_url=None, redoc_url=None)
    app.state.jobs = registry

    @app.get("/")
    async def index():
        if not UI_FILE.is_file():
            return JSONResponse({"error": "flasher_ui.html is missing"}, status_code=500)
        return FileResponse(UI_FILE, media_type="text/html")

    @app.get("/api/state")
    async def state():
        tool_status = {}
        for name, module in (("esptool", "esptool"), ("mpremote", "mpremote"),
                             ("pyserial", "serial")):
            tool_status[name] = _module_available(module)
        return {
            "ports": list_ports(),
            "device_types": load_device_types(),
            "defaults": {
                "server": DEFAULT_HUB,
                "hub_username": "admin",
                "baud": DEFAULT_BAUD,
            },
            "tools": tool_status,
            "firmware_dir": str(FIRMWARE_DIR),
        }

    @app.post("/api/probe")
    async def probe(body: Dict[str, Any]):
        port = (body or {}).get("port")
        if not port:
            return JSONResponse({"error": "port is required"}, status_code=400)

        # A BOOTSEL volume is not a serial port; esptool cannot open it.
        if board_kind(str(port)) == "uf2":
            info = Path(str(port)) / UF2_INFO_FILE
            if not info.is_file():
                return {"port": port, "kind": "uf2", "ok": False, "chip": None,
                        "micropython": False,
                        "output": "%s has no %s — is the board in BOOTSEL mode?"
                                  % (port, UF2_INFO_FILE)}
            model = ""
            try:
                for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.lower().startswith("model:"):
                        model = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass
            return {"port": port, "kind": "uf2", "ok": True, "chip": "RP2",
                    "model": model, "micropython": False, "output": ""}

        detect_runner = runner or Runner()
        code, output = detect_runner.capture(
            [sys.executable, "-m", "esptool", "--port", port, esptool_cmd("flash_id", detect_runner)],
            timeout=40,
        )
        chip = parse_chip(output)
        micropython = False
        if not chip:
            # esptool cannot drive a board that is already running MicroPython,
            # so report that case specifically instead of "port busy".
            code2, out2 = detect_runner.capture(
                [sys.executable, "-m", "mpremote", "connect", port, "exec", "print('MPY')"],
                timeout=25,
            )
            if code2 == 0 and "MPY" in out2:
                micropython = True
                return {
                    "port": port, "kind": "serial", "chip": None,
                    "micropython": True, "ok": True, "output": "",
                    "notice": "MicroPython is already running on this board. "
                              "Flashing will restart it into the bootloader "
                              "automatically.",
                }
        if chip:
            code2, out2 = detect_runner.capture(
                [sys.executable, "-m", "mpremote", "connect", port, "exec", "print('MPY')"],
                timeout=25,
            )
            micropython = code2 == 0 and "MPY" in out2
        return {
            "port": port,
            "kind": "serial",
            "chip": chip,
            "micropython": micropython,
            "ok": bool(chip),
            "output": "\n".join(output.strip().splitlines()[-6:]) if not chip else "",
        }

    @app.post("/api/configure")
    async def configure(body: Dict[str, Any]):
        """Rewrite config.json/devices.json on a board already running MicroPython.

        No erase, no firmware download: the two JSON files are replaced over the
        existing session and the board reboots.  This is the fix for a working
        board that joined the wrong network or needs new device settings.
        """
        body = body or {}
        port = str(body.get("port") or "").strip()
        if not port:
            return JSONResponse({"error": "select a serial port"}, status_code=400)
        if board_kind(port) == "uf2":
            return JSONResponse(
                {"error": "that is a BOOTSEL drive; configure a board that is "
                          "running MicroPython, or flash it first"},
                status_code=400,
            )
        try:
            problem = _validate_request(body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)

        configure_runner = runner or Runner()
        code, output = configure_runner.capture(
            [sys.executable, "-m", "mpremote", "connect", port, "exec", "print('MPY')"],
            timeout=25,
        )
        if code != 0 or "MPY" not in output:
            return JSONResponse(
                {"error": "no MicroPython running on %s. Unplug the USB cable, "
                          "wait a few seconds, plug it back in, then try again."
                          % port},
                status_code=400,
            )

        config = {
            "wifi_ssid": body.get("wifi_ssid", ""),
            "wifi_pass": body.get("wifi_pass", ""),
            "server": str(body.get("server", "")).rstrip("/"),
            "node_id": body.get("node_id") or "",
            "token": body.get("token") or "",
            "heartbeat_s": 10,
            "report_s": 30,
        }
        devices = [{
            "type": body["device_type"],
            "uid": body["device_uid"],
            "name": body.get("device_name") or body["device_uid"],
            "config": body.get("device_config") or {},
        }]

        # Never blank an existing token.  The form has no token field -- only a
        # flash job resolves one -- so writing "" here would lock the board out
        # of the hub, which is exactly what happened once.
        if not config["token"]:
            code, existing = configure_runner.capture(
                [sys.executable, "-m", "mpremote", "connect", port, "exec",
                 "import json;print('TOK',json.load(open('/config.json')).get('token',''))"],
                timeout=25,
            )
            if code == 0 and "TOK" in existing:
                line = [piece for piece in existing.splitlines() if "TOK" in piece][0]
                previous = line.split("TOK", 1)[1].strip().strip("'\"")
                if previous:
                    config["token"] = previous
                    job_token_note = "kept the token already on the board"
                else:
                    job_token_note = ""
            else:
                job_token_note = ""
        else:
            job_token_note = "used the token from the request"

        staging = Path(tempfile.mkdtemp(prefix="shcfg-"))
        try:
            for name, payload in (("config.json", config), ("devices.json", devices)):
                local = staging / name
                local.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                code, out = configure_runner.capture(
                    [sys.executable, "-m", "mpremote", "connect", port,
                     "fs", "cp", str(local), ":" + name],
                    timeout=120,
                )
                if code != 0:
                    return JSONResponse(
                        {"error": "could not write %s: %s" % (name, out.strip()[-200:])},
                        status_code=500,
                    )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        configure_runner.capture(
            [sys.executable, "-m", "mpremote", "connect", port, "reset"],
            timeout=30,
        )
        return {"ok": True, "port": port, "node_id": config["node_id"],
                "token": bool(config["token"]), "token_note": job_token_note}

    @app.post("/api/flash")
    async def flash(body: Dict[str, Any]):
        try:
            problem = _validate_request(body or {})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        request = dict(body)
        job = FlashJob(request, runner=runner)
        registry[job.id] = job

        # Resolve the token up front so a bad password fails before erasing.
        try:
            _attach_token(job)
        except FlashError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        thread = threading.Thread(target=job.run, name="flash-%s" % job.id, daemon=True)
        thread.start()
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}")
    async def job_state(job_id: str):
        job = registry.get(job_id)
        if job is None:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return job.state()

    @app.get("/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str):
        job = registry.get(job_id)
        if job is None:
            return JSONResponse({"error": "no such job"}, status_code=404)

        async def events():
            import asyncio

            sent = 0
            while True:
                state = job.state()
                log = state["log"]
                if sent < len(log):
                    chunk = log[sent:]
                    sent = len(log)
                    for entry in chunk:
                        yield "data: %s\n\n" % json.dumps({"log": entry})
                if state["finished"]:
                    yield "data: %s\n\n" % json.dumps({"done": True, "state": state})
                    return
                yield "data: %s\n\n" % json.dumps({"steps": state["steps"]})
                await asyncio.sleep(0.4)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    return app


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _validate_request(body: Dict[str, Any]) -> str:
    port = str(body.get("port") or "").strip()
    if not port:
        return "select a serial port or a BOOTSEL drive"
    # A drive path (R:\) is a UF2 board; anything else goes through esptool.
    kind = str(body.get("port_kind") or "").strip()
    if not kind:
        kind = board_kind(port)
    if kind not in ("serial", "uf2"):
        return "unknown board type %r" % kind
    body["port_kind"] = kind

    for key, label in (("server", "home server address"), ("wifi_ssid", "Wi-Fi name"),
                       ("device_type", "device type"), ("device_uid", "device id")):
        if not str(body.get(key) or "").strip():
            return "%s is required" % label
    server = str(body["server"]).strip()
    if not re.match(r"^https?://[^\s/]+", server):
        return "home server address must look like http://10.0.0.105:8080"
    uid = str(body["device_uid"]).strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", uid):
        return ("device id must be 1-64 characters: letters, digits, dot, dash "
                "or underscore")
    types = {t["id"]: t for t in load_device_types()}
    if body["device_type"] not in types:
        return "unknown device type %r" % body["device_type"]
    try:
        body["device_config"] = validate_config(types[body["device_type"]],
                                                body.get("device_config") or {})
    except FlashError as exc:
        return str(exc)
    # Default node id reflects the board family so two boards do not collide.
    prefix = "pico" if kind == "uf2" else "esp32"
    body["node_id"] = (str(body.get("node_id") or "").strip()
                       or "%s-%s" % (prefix, uid))
    return ""


def _attach_token(job: FlashJob) -> None:
    """Trade the hub credentials for this node's device token."""
    req = job.request
    username = str(req.get("hub_username") or "").strip()
    password = str(req.get("hub_password") or "")
    server = str(req.get("server") or "").rstrip("/")
    node_id = req["node_id"]
    if not username or not password:
        job.emit("warn", "no hub credentials: the board must be enrolled from the UI",
                 "detect")
        req["token"] = ""
        return
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    login = urllib.request.Request(
        server + "/api/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with opener.open(login, timeout=15) as response:
            if response.status != 200:
                raise FlashError("hub login failed (%s)" % response.status)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        raise FlashError("hub login failed: %s%s" % (exc.code, " - " + detail if detail else ""))
    except Exception as exc:  # noqa: BLE001
        raise FlashError("cannot reach the hub at %s: %s" % (server, exc))

    token_request = urllib.request.Request(
        "%s/api/nodes/%s/token" % (server, node_id),
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with opener.open(token_request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FlashError("could not get a device token: %s" % exc)
    req["token"] = payload.get("token", "")
    job.emit("info", "got a device token for %s" % node_id, "detect")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SmartHome board flasher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    args = parser.parse_args(argv)

    missing = [name for name in ("esptool", "mpremote", "serial") if not _module_available(name)]
    if missing:
        human = {"serial": "pyserial"}.get(missing[0], missing[0])
        print("missing: %s\n  python -m pip install esptool mpremote pyserial" %
              ", ".join(human if m == "serial" else m for m in missing), file=sys.stderr)
        return 2

    import uvicorn

    app = create_app()
    url = "http://%s:%d" % (args.host, args.port)
    print("SmartHome flasher on %s" % url)
    if args.open:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
