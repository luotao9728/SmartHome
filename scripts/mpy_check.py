"""Compile every firmware file with the real MicroPython interpreter.

Catches the class of bug a CPython test can never see: syntax MicroPython does
not support (``__future__`` imports, typing-only syntax, ``match``, ...).

    python scripts/mpy_check.py [--interpreter path/to/micropython]

Exit code 0 means every file compiled.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "micropython"
CANDIDATES = [
    ROOT / "mpyenv" / "Library" / "bin" / "micropython.exe",
    ROOT / "mpyenv" / "bin" / "micropython",
    pathlib.Path("/usr/bin/micropython"),
]

CHECK_SNIPPET = "try:\n    compile(open(%r).read(), %r, 'exec')\nexcept SyntaxError as e:\n    print('FAIL', %r, e)\n    raise SystemExit(1)\n"


def find_interpreter(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in CANDIDATES:
        if candidate.exists():
            return str(candidate)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("micropython", "micropython.exe"):
            candidate = pathlib.Path(directory) / name
            if candidate.is_file():
                return str(candidate)
    raise SystemExit(
        "no MicroPython interpreter found.\n"
        "Install one with:  conda install -c conda-forge micropython\n"
        "or pass --interpreter PATH."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compile the firmware with MicroPython")
    parser.add_argument("--interpreter", default=None)
    args = parser.parse_args(argv)

    interpreter = find_interpreter(args.interpreter)
    files = sorted(
        path for path in FIRMWARE.rglob("*.py") if "__pycache__" not in path.parts
    )
    if not files:
        raise SystemExit("no firmware files found")

    failures = []
    for path in files:
        snippet = CHECK_SNIPPET % (str(path), str(path), str(path))
        result = subprocess.run([interpreter, "-c", snippet], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip().splitlines()
            failures.append((path, detail[-1] if detail else "exit %d" % result.returncode))

    for path, detail in failures:
        print("FAIL %s: %s" % (path.relative_to(ROOT), detail))
    print("%d/%d firmware files compile under %s" % (len(files) - len(failures), len(files), interpreter))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
