"""Tests for the device type registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smarthome.registry import ManifestError, TypeRegistry


@pytest.fixture()
def types_dir(tmp_path: Path) -> Path:
    path = tmp_path / "types"
    path.mkdir()
    return path


def write_type(directory: Path, name: str, manifest: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_bundled_types_load(registry):
    ids = registry.ids()
    assert "pwm_led_strip" in ids
    assert "relay" in ids
    assert registry.errors == {}
    strip = registry.require("pwm_led_strip")
    assert strip.category == "light"
    assert strip.properties["brightness"]["max"] == 100
    assert strip.default_props()["power"] is False
    assert strip.default_config()["pin"] == 4
    assert "identify" in strip.actions


def test_unknown_type_is_none(registry):
    assert registry.get("does-not-exist") is None
    with pytest.raises(KeyError):
        registry.require("does-not-exist")


def test_invalid_manifest_is_reported_not_fatal(types_dir: Path):
    write_type(types_dir, "good.json", {"id": "good_type", "name": "Good"})
    write_type(types_dir, "bad_id.json", {"id": "Bad Type!"})
    write_type(types_dir, "bad_prop.json",
               {"id": "bad_prop", "properties": {"x": {"type": "int", "min": 5, "max": 1}}})
    write_type(types_dir, "bad_json.json", {"oops": True})
    (types_dir / "broken.json").write_text("{not json", encoding="utf-8")
    registry = TypeRegistry([types_dir])
    errors = registry.reload()
    assert registry.ids() == ["good_type"]
    assert set(errors) == {"bad_id.json", "bad_prop.json", "bad_json.json", "broken.json"}


def test_duplicate_ids_are_rejected(types_dir: Path):
    write_type(types_dir, "a.json", {"id": "dup_type"})
    write_type(types_dir, "b.json", {"id": "dup_type"})
    registry = TypeRegistry([types_dir])
    errors = registry.reload()
    assert registry.ids() == ["dup_type"]
    assert len(errors) == 1


def test_enum_requires_options_and_valid_default(types_dir: Path):
    write_type(types_dir, "e1.json", {"id": "enum1", "properties": {"m": {"type": "enum"}}})
    write_type(types_dir, "e2.json", {"id": "enum2",
                                      "properties": {"m": {"type": "enum", "options": ["a"],
                                                           "default": "b"}}})
    registry = TypeRegistry([types_dir])
    errors = registry.reload()
    assert set(errors) == {"e1.json", "e2.json"}


def test_control_inference(types_dir: Path):
    write_type(types_dir, "c.json", {
        "id": "controls",
        "properties": {
            "a": {"type": "bool"},
            "b": {"type": "int", "min": 0, "max": 10},
            "c": {"type": "enum", "options": ["x", "y"]},
            "d": {"type": "string", "control": "color"},
        },
    })
    registry = TypeRegistry([types_dir])
    registry.reload()
    dtype = registry.require("controls")
    assert dtype.properties["a"]["control"] == "switch"
    assert dtype.properties["b"]["control"] == "slider"
    assert dtype.properties["c"]["control"] == "select"
    assert dtype.properties["c"]["default"] == "x"
    assert dtype.properties["d"]["control"] == "color"


def test_validate_config_fills_defaults_and_rejects_unknown(registry):
    strip = registry.require("pwm_led_strip")
    assert strip.validate_config({})["pin"] == 4
    with pytest.raises(ManifestError):
        strip.validate_config({"nope": 1})
    with pytest.raises(ManifestError):
        strip.validate_config({"pin": 999})
    assert strip.validate_config({"pin": "12"})["pin"] == 12


def test_validate_config_lenient_mode_keeps_known_fields(registry):
    """A node reporting one extra config key must not lose every setting."""
    strip = registry.require("pwm_led_strip")
    config = strip.validate_config(
        {"pin": "7", "gamma": 1.8, "frequency_hz": 1000}, strict=False
    )
    assert config["pin"] == 7
    assert config["gamma"] == 1.8
    assert config["max_duty"] == 100        # filled from the manifest
    assert "frequency_hz" not in config     # dropped, it is a property


def test_lenient_mode_falls_back_per_field(registry):
    """In lenient mode a bad value falls back to its default, keeping the rest."""
    strip = registry.require("pwm_led_strip")
    config = strip.validate_config({"pin": 999, "gamma": 1.5}, strict=False)
    assert config["pin"] == 4        # out of range -> manifest default
    assert config["gamma"] == 1.5    # the good field survives
    assert config["max_duty"] == 100


def test_validate_action_args(registry):
    relay = registry.require("relay")
    assert relay.validate_args("pulse", {}) == {"ms": 500}
    assert relay.validate_args("pulse", {"ms": "1200"}) == {"ms": 1200}
    with pytest.raises(ManifestError):
        relay.validate_args("pulse", {"ms": 5})
    with pytest.raises(ManifestError):
        relay.validate_args("no_such_action", {})


def test_manifest_ordering(registry):
    ordered = [t.id for t in registry.all()]
    assert ordered.index("pwm_led_strip") < ordered.index("temperature_sensor")


def test_public_manifest_is_json_serialisable(registry):
    payload = json.dumps(registry.public())
    assert "pwm_led_strip" in payload
