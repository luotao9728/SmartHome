"""Clean-flash an ESP32 from scratch, with no buttons and no web UI.

Recovery path for a board whose USB stack is wedged, or that has no BOOT button
(the ESP32-C6 devkit has none).  It drives esptool directly and tries every
reset strategy before giving up, so it works whether the board is running
MicroPython, sitting in the ROM bootloader, or in an unknown state.

    python tools/clean_flash.py                 # find the board, ask what to do
    python tools/clean_flash.py --port COM7     # use a specific port
    python tools/clean_flash.py --erase-only    # just wipe it
    python tools/clean_flash.py --chip esp32c6 --yes

Exactly one physical step may be needed: unplug the USB cable and plug it back
in.  That is the only true reset on a board without a button, and it clears a
wedged USB-Serial-JTAG peripheral.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.flasher import (  # noqa: E402
    DEFAULT_BAUD,
    FlashError,
    Runner,
    chip_family,
    download_firmware,
    esptool_cmd,
    list_ports,
    parse_chip,
)

#: Chips whose USB peripheral is part of the SoC (no CP210x/CH340 bridge).
#: These can enter the ROM downloader without BOOT being held.
NATIVE_USB = {"ESP32-C6", "ESP32-C3", "ESP32-S3", "ESP32-S2", "ESP32-H2"}

#: Reset strategies to try, in order.  esptool's --before controls how it
#: gets the chip into the downloader.
BEFORE_MODES = [
    ("usb-reset", "reset through USB (works on native-USB boards, no buttons)"),
    ("default-reset", "reset over the serial control lines"),
    ("no-reset", "assume it is already in the bootloader"),
]


def say(message: str) -> None:
    print(message, flush=True)


def rule(title: str = "") -> None:
    if title:
        print("\n== %s %s" % (title, "=" * max(0, 60 - len(title))), flush=True)
    else:
        print("-" * 64, flush=True)


def find_board(explicit: Optional[str]) -> Optional[str]:
    """Return a port that looks like an ESP32, preferring native USB."""
    ports = list_ports()
    if explicit:
        return explicit
    candidates = [p for p in ports if p.get("kind", "serial") == "serial"]
    # 303A:1001 is the ROM bootloader, 303A:400x is the MicroPython CDC link.
    espressif = [p for p in candidates if "303A" in (p.get("hwid") or "").upper()]
    if espressif:
        return espressif[0]["device"]
    if candidates:
        return candidates[0]["device"]
    return None


def describe_ports() -> None:
    ports = list_ports()
    if not ports:
        rule("serial ports")
        say("  none found. Is the cable plugged in? A charge-only cable will not work.")
        return
    rule("serial ports")
    for port in ports:
        note = ""
        hwid = (port.get("hwid") or "").upper()
        if "303A:1001" in hwid:
            note = "  <- ESP32 ROM bootloader (ready)"
        elif "303A:400" in hwid or "303A:000" in hwid:
            note = "  <- ESP32 running firmware (USB CDC)"
        say("  %-6s %-38s%s" % (port["device"], (port["description"] or "")[:38], note))


def run_esptool(args: List[str], timeout: float = 180.0) -> "tuple[int, str]":
    """Run esptool and return (exit code, combined output)."""
    command = [sys.executable, "-m", "esptool"] + args
    say("  $ esptool " + " ".join(args))
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, errors="replace",
        )
    except subprocess.TimeoutExpired:
        say("    timed out after %.0fs" % timeout)
        return 1, "timed out"
    except OSError as exc:
        return 1, str(exc)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output


def probe(port: str, chip_hint: Optional[str]) -> "tuple[Optional[str], str]":
    """Try every reset mode until esptool identifies the chip."""
    say("probing %s" % port)
    last_output = ""
    for mode, explanation in BEFORE_MODES:
        rule("trying --before %s" % mode)
        say("  %s" % explanation)
        args = ["--port", port, "--before", mode]
        if chip_hint:
            args += ["--chip", chip_hint]
        args += [esptool_cmd("flash_id", _runner())]
        code, output = run_esptool(args, timeout=90)
        last_output = output
        chip = parse_chip(output)
        if chip:
            say("\n  found %s" % chip)
            return chip, output
        reason = _first_error(output)
        if reason:
            say("  -> %s" % reason)
        time.sleep(1.0)
    return None, last_output


_shared_runner = Runner()


def _runner() -> Runner:
    return _shared_runner


def _first_error(output: str) -> str:
    """The most useful line from esptool's output."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    for line in lines:
        upper = line.upper()
        if "ERROR" in upper or "FATAL" in upper or "FAILED" in upper:
            return line[:160]
    return lines[-1][:160] if lines else ""


def erase(port: str, chip: Optional[str]) -> bool:
    rule("erasing flash")
    args = ["--port", port, "--before", "usb-reset", "--after", "hard-reset"]
    if chip:
        args += ["--chip", chip]
    args += [esptool_cmd("erase_flash", _runner())]
    code, output = run_esptool(args, timeout=240)
    if code == 0:
        say("  flash erased")
        return True
    say("  erase failed: %s" % _first_error(output))
    return False


def write(port: str, chip: str, binary: Path, baud: int) -> bool:
    rule("writing MicroPython")
    for speed in (baud, DEFAULT_BAUD, 115200):
        args = ["--port", port, "--before", "usb-reset", "--after", "hard-reset",
                "--chip", chip, "--baud", str(speed),
                esptool_cmd("write_flash", _runner()), "0", str(binary)]
        code, output = run_esptool(args, timeout=600)
        if code == 0:
            say("  wrote %s at %d baud" % (binary.name, speed))
            return True
        say("  failed at %d baud: %s" % (speed, _first_error(output)))
        time.sleep(1.5)
    return False


def verify(port: str, chip: str) -> bool:
    """Confirm the board now runs MicroPython."""
    rule("verifying")
    say("  waiting for the board to boot MicroPython...")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        time.sleep(2.0)
        for candidate in _serial_ports():
            code, output = _mpremote(candidate, "exec", "print('MPY-READY')")
            if code == 0 and "MPY-READY" in output:
                say("  MicroPython is running on %s" % candidate)
                return True
    say("  MicroPython did not answer yet")
    return False


def _serial_ports() -> List[str]:
    return [p["device"] for p in list_ports() if p.get("kind", "serial") == "serial"]


def _mpremote(port: str, *args: str) -> "tuple[int, str]":
    command = [sys.executable, "-m", "mpremote", "connect", port] + list(args)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=25,
            stdin=subprocess.DEVNULL, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clean-flash an ESP32 from scratch, no buttons needed")
    parser.add_argument("--port", default=None, help="serial port, e.g. COM7")
    parser.add_argument("--chip", default=None, help="force a chip, e.g. esp32c6")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--erase-only", action="store_true",
                        help="only erase; do not install MicroPython")
    parser.add_argument("--keep-firmware", action="store_true",
                        help="keep the cached .bin instead of re-downloading")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    say("SmartHome clean flasher")
    rule()
    describe_ports()

    port = find_board(args.port)
    if not port:
        rule("no board")
        say("Plug the board in with a USB *data* cable (many are charge-only)")
        say("and run this again.")
        return 2

    chip_hint = args.chip
    chip, output = probe(port, chip_hint)

    if not chip:
        rule("could not reach the board")
        reason = _first_error(output)
        if reason:
            say("  esptool says: %s" % reason)

        # A missing tool and a wedged board need completely different fixes;
        # blaming the hardware when the module is simply absent wastes an hour.
        if "no module named" in reason.lower():
            say("")
            say("esptool is not installed for THIS interpreter:")
            say("  %s" % sys.executable)
            say("")
            say("Install it where you run this command:")
            say("  %s -m pip install esptool mpremote" % sys.executable)
            say("")
            say("Note that the venv and the system Python are separate: if")
            say("esptool works with 'python' but not with '.venv\\Scripts\\python.exe',")
            say("install it into the venv as shown above.")
            return 7

        if "write timeout" in reason.lower() or "clearecomerror" in reason.lower():
            say("")
            say("The port opens but the board never answers, which means the USB")
            say("peripheral needs a power cycle. Do this, then run again:")
            say("")
            say("  1. Unplug the USB cable from the BOARD end")
            say("  2. Wait 5 seconds")
            say("  3. Plug it back in")
            say("  4. Run this command again immediately")
            say("")
            say("There is no button on this board, so unplugging is the only true")
            say("reset. If it still fails, try another USB port or a different")
            say("cable -- a charge-only cable cannot flash anything.")
            return 3

        say("")
        say("Could not identify a chip. Try:")
        say("")
        say("  * unplug and replug the USB cable")
        say("  * a different USB port or cable (data, not charge-only)")
        say("  * --chip esp32s3 if autodetection is being difficult")
        return 3

    if not args.yes:
        family = chip_family(chip)
        say("")
        say("About to ERASE the whole flash of the %s on %s." % (chip, port))
        say("Everything on the board will be lost.")
        if input("Type 'yes' to continue: ").strip().lower() not in ("yes", "y"):
            say("cancelled")
            return 1

    if not erase(port, chip):
        rule("erase failed")
        say("Unplug the USB cable, wait 5 seconds, plug it back in, then run")
        say("this command again. Do not open a serial monitor in between.")
        return 4

    if args.erase_only:
        rule("done")
        say("The board is wiped. Run without --erase-only to install MicroPython.")
        return 0

    family = chip_family(chip)
    say("")
    say("downloading MicroPython for %s..." % family)
    try:
        binary = download_firmware(family, lambda line: say("  " + line))
    except FlashError as exc:
        rule("firmware download failed")
        say("  %s" % exc)
        return 5

    # The board re-enumerates after an erase; give it a moment and re-detect.
    time.sleep(2.0)
    for candidate in _serial_ports():
        if candidate != port:
            say("board re-enumerated as %s" % candidate)
            port = candidate
            break

    if not write(port, chip, binary, args.baud):
        rule("write failed")
        say("Unplug the USB cable, wait 5 seconds, plug it back in, then run")
        say("this command again.")
        return 6

    if verify(port, chip):
        rule("success")
        say("MicroPython %s is installed." % binary.stem)
        say("")
        say("Next: open the web flasher and press Flash Micro Controller to")
        say("install the SmartHome firmware and write config.json, or run")
        say("  python tools/flash_node.py --port %s --server ..." % port)
        return 0

    rule("flashed, but not verified")
    say("The image was written. Unplug and replug the board, then check:")
    say("  python -m mpremote connect %s repl" % port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
