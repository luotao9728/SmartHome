"""Unit tests for the protocol layer."""

from __future__ import annotations

import pytest

from smarthome import protocol


MANIFEST = {
    "id": "demo",
    "properties": {
        "power": {"type": "bool", "default": False},
        "brightness": {"type": "int", "min": 0, "max": 100, "default": 100},
        "temp": {"type": "float", "min": -10, "max": 50, "default": 20.0, "settable": False},
        "mode": {"type": "enum", "options": ["a", "b"], "default": "a"},
        "color": {"type": "string", "control": "color", "default": "#ffffff"},
    }
}


def test_defaults_from_manifest():
    # ``temp`` is read-only, so it is deliberately absent: a sensor reading is
    # not something the user can ask for.
    assert protocol.default_props(MANIFEST) == {
        "power": False,
        "brightness": 100,
        "mode": "a",
        "color": "#ffffff",
    }


def test_readonly_properties_never_enter_desired(registry):
    for dtype in registry.all():
        defaults = dtype.default_props()
        for key in defaults:
            assert dtype.properties[key].get("settable", True), \
                "%s.default_props() leaked read-only property %r" % (dtype.id, key)


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"power": "on"}, {"power": True}),
        ({"power": 0}, {"power": False}),
        ({"brightness": "42"}, {"brightness": 42}),
        ({"brightness": 42.0}, {"brightness": 42}),
        ({"power": "0"}, {"power": False}),
        ({"color": "#AABBCC"}, {"color": "#AABBCC"}),
    ],
)
def test_normalize_accepts_reasonable_input(values, expected):
    clean, errors = protocol.normalize_props(MANIFEST, values)
    assert errors == []
    assert clean == expected


@pytest.mark.parametrize(
    "values,fragment",
    [
        ({"brightness": 101}, "must be <="),
        ({"brightness": -1}, "must be >="),
        ({"brightness": "abc"}, "whole number"),
        ({"color": "red"}, "#RRGGBB"),
        ({"mode": "c"}, "expected one of"),
        ({"nope": 1}, "unknown property"),
        ({"temp": 1.0}, "read only"),
    ],
)
def test_normalize_rejects_bad_input(values, fragment):
    clean, errors = protocol.normalize_props(MANIFEST, values)
    assert clean == {}
    assert errors and fragment in errors[0]


def test_reported_state_ignores_unknown_and_readonly():
    clean, errors = protocol.normalize_props(
        MANIFEST,
        {"temperature": 1, "firmware_field": 2, "power": True},
        only_settable=False,
        ignore_unknown=True,
    )
    assert errors == []
    assert clean == {"power": True}


def test_bool_equality_does_not_confuse_one_and_true():
    assert protocol.values_equal(True, True)
    assert not protocol.values_equal(1, True)


def test_diff_desired_only_lists_differences():
    desired = {"power": True, "brightness": 50}
    reported = {"power": True, "brightness": 10}
    assert protocol.diff_desired(MANIFEST, desired, reported) == {"brightness": 50}
    # Properties the device never reported are left alone...
    assert protocol.diff_desired(MANIFEST, {"mode": "b"}, {}) == {}
    # ...and read-only ones are never pushed.
    assert protocol.diff_desired(MANIFEST, {"temp": 1.0}, {"temp": 20.0}) == {}


def test_message_builders_are_minimal_and_json_safe():
    import json

    for message in (
        protocol.cmd("c1", "strip", {"power": True}),
        protocol.act("a1", "strip", "identify"),
        protocol.ping(1, 123.456789),
        protocol.state_req("s1"),
        protocol.hello({"heartbeat_s": 10, "report_s": 30, "ack_timeout_s": 3}, node_id="n1", server_time=1.5),
    ):
        assert json.loads(json.dumps(message)) == message


def test_parse_hello_requires_devices_and_types():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_hello({})
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_hello({"devices": [{"uid": "x"}]})
    parsed = protocol.parse_hello(
        {
            "node": "n1",
            "board": "esp32-c6",
            "fw": "1.0",
            "ip": "10.0.0.5",
            "rssi": -55,
            "devices": [{"uid": "x", "type": "relay", "state": {"power": True}}],
        }
    )
    assert parsed["node_id"] == "n1"
    assert parsed["devices"][0]["state"] == {"power": True}


def test_parse_ack_and_state_validation():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_state({"t": "state"})
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_ack({"id": "x", "ok": "yes"})
    assert protocol.parse_ack({"id": "x", "ok": False, "error": "nope"})["error"] == "nope"


def test_parse_node_message_rejects_non_objects():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_node_message([1, 2])
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_node_message({})
