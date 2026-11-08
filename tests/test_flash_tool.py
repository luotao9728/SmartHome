"""Verify the flashing tool stages a valid firmware tree (no board required)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import flash_node  # noqa: E402


@pytest.fixture()
def staging():
    tree = flash_node.build(None)
    try:
        yield tree
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def test_staging_contains_the_whole_firmware(staging: Path):
    names = {path.relative_to(staging).as_posix() for path in staging.rglob("*.py")}
    assert {"boot.py", "main.py"} <= names
    assert "shnode/agent.py" in names
    assert "shnode/transport.py" in names
    assert "shnode/devices/pwm_led_strip.py" in names


def test_staging_has_no_bytecode_or_caches(staging: Path):
    for path in staging.rglob("__pycache__"):
        raise AssertionError("staged %s" % path)
    assert not list(staging.rglob("*.pyc"))


def test_all_staged_files_compile(staging: Path):
    for path in staging.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_device_list_injection_keeps_valid_python():
    devices = '[{"type": "relay", "uid": "pump", "config": {"pin": 5}}]'
    tree = flash_node.build(devices)
    try:
        text = (tree / "main.py").read_text(encoding="utf-8")
        compile(text, "main.py", "exec")
        marker = text.index("DEVICES = ")
        end = text.index("]", marker) + 1
        line = text[marker:end]
        assert '"uid": "pump"' in line
        assert "REBOOT_ON_ERROR" in text       # the rest of the file survives
        assert text.count("DEVICES = ") == 1
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def test_unused_imports_do_not_creep_back():
    """The firmware must stay lint clean; a stray import costs heap on a board."""
    try:
        import pyflakes.api  # type: ignore
        import pyflakes.reporter  # type: ignore
    except ImportError:
        pytest.skip("pyflakes is not installed")
    import io

    from pyflakes import api, reporter

    output = io.StringIO()
    errors = io.StringIO()
    count = api.checkRecursive(
        [str(ROOT / "micropython")],
        reporter.Reporter(output, errors),
    )
    assert count == 0, output.getvalue() + errors.getvalue()
