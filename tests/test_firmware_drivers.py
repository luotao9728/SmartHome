"""Driver tests: the firmware runs on CPython (with simulated hardware).

These are the tests that keep the PWM/LED behaviour honest -- gamma, fading,
inversion, clamping -- and they double as the "same code on the desk as on the
board" guarantee.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parent.parent / "micropython"
if str(FIRMWARE) not in sys.path:
    sys.path.insert(0, str(FIRMWARE))

from shnode.device import Device, DeviceError  # noqa: E402
from shnode.devices.button import Button  # noqa: E402
from shnode.devices.pwm_led_strip import PwmLedStrip, gamma_lut  # noqa: E402
from shnode.devices.relay import Relay  # noqa: E402
from shnode.devices.rgb_led_strip import RgbLedStrip, parse_color, to_hex  # noqa: E402
from shnode.devices.temperature_sensor import TemperatureSensor  # noqa: E402


# --------------------------------------------------------------------------- #
# gamma / mapping
# --------------------------------------------------------------------------- #
def test_gamma_table_is_monotonic_and_normalised():
    table = gamma_lut(2.2, 65535)
    assert len(table) == 101
    assert table[0] == 0
    assert table[100] == 65535
    assert all(a <= b for a, b in zip(table, table[1:]))
    # Gamma 2.2 puts 50 % at about 22 % of full duty.
    assert 13000 < table[50] < 15000


def test_gamma_one_is_linear():
    table = gamma_lut(1.0, 1000)
    assert table[0] == 0 and table[50] == 500 and table[100] == 1000


def test_gamma_is_steep_for_high_exponents():
    steep = gamma_lut(3.0, 65535)
    linear = gamma_lut(1.0, 65535)
    assert steep[50] < linear[50]


# --------------------------------------------------------------------------- #
# PWM strip
# --------------------------------------------------------------------------- #
def make_strip(**config):
    base = {"pin": 4}
    base.update(config)
    strip = PwmLedStrip("strip-1", base)
    strip.setup()
    strip._fade_ms = 0  # instant, so assertions are synchronous
    return strip


def test_strip_starts_off():
    strip = make_strip()
    try:
        assert strip.snapshot()["power"] is False
        assert strip._pwm.duty_u16() == 0
    finally:
        strip.teardown()


def test_strip_maps_brightness_through_gamma():
    strip = make_strip()
    try:
        strip.apply({"power": True, "brightness": 100})
        assert strip._pwm.duty_u16() == strip._table[100]
        strip.apply({"brightness": 50})
        assert strip._pwm.duty_u16() == strip._table[50]
        strip.apply({"brightness": 0})
        assert strip._pwm.duty_u16() == 0
    finally:
        strip.teardown()


def test_strip_brightness_while_off_stays_dark():
    strip = make_strip()
    try:
        strip.apply({"brightness": 90})
        assert strip.snapshot()["power"] is False
        assert strip._pwm.duty_u16() == 0
        strip.apply({"power": True})
        assert strip._pwm.duty_u16() == strip._table[90]
    finally:
        strip.teardown()


def test_strip_frequency_change_reaches_hardware():
    strip = make_strip()
    try:
        strip.apply({"frequency_hz": 2000})
        assert strip.snapshot()["frequency_hz"] == 2000
        assert strip._pwm.freq() == 2000
        strip.apply({"frequency_hz": 5})          # clamped
        assert strip._pwm.freq() == 60
        strip.apply({"frequency_hz": 999999})     # clamped
        assert strip._pwm.freq() == 20000
    finally:
        strip.teardown()


def test_strip_active_low_inverts_the_duty():
    strip = make_strip(active_low=True)
    try:
        strip.apply({"power": True, "brightness": 100})
        assert strip._pwm.duty_u16() == 0            # full on == 0 V on gate driver
        strip.apply({"power": False})
        assert strip._pwm.duty_u16() == 65535        # off == fully pulled up
    finally:
        strip.teardown()


def test_strip_max_duty_limit():
    strip = make_strip(max_duty=60)
    try:
        strip.apply({"power": True, "brightness": 100})
        assert strip._pwm.duty_u16() <= int(65535 * 0.6)
    finally:
        strip.teardown()


@pytest.mark.parametrize("props", [{"brightness": 101}, {"brightness": -1}, {"nope": 1}])
def test_strip_rejects_bad_properties(props):
    strip = make_strip()
    try:
        with pytest.raises(DeviceError):
            strip.apply(dict(props))
    finally:
        strip.teardown()


def test_strip_fade_runs_as_a_task():
    strip = make_strip()
    strip._fade_ms = 120
    try:
        with pytest.raises(RuntimeError):
            # Fading needs a running event loop; on a board the agent provides
            # one, and outside a loop the driver fails loudly instead of
            # silently leaving the duty cycle mid-fade.
            strip.apply({"power": True, "brightness": 100})
    finally:
        strip._fade_ms = 0
        strip.teardown()


async def test_strip_fade_reaches_the_target():
    strip = make_strip()
    strip.attach(None)
    strip._fade_ms = 60
    try:
        strip.apply({"power": True, "brightness": 100})
        for _ in range(40):
            await asyncio.sleep(0.02)
            if strip._current_duty == strip._table[100]:
                break
        assert strip._current_duty == strip._table[100]
    finally:
        strip.teardown()


async def test_strip_identify_action_restores_state():
    strip = make_strip()
    try:
        strip.apply({"power": True, "brightness": 40})
        before = strip._current_duty
        await strip.action_identify()
        assert strip._current_duty == before
        assert strip.snapshot()["brightness"] == 40
    finally:
        strip.teardown()


async def test_strip_toggle_action():
    strip = make_strip()
    try:
        strip.apply({"power": False, "brightness": 70})
        await strip.action_toggle()
        assert strip.snapshot()["power"] is True
        assert strip._current_duty == strip._table[70]
    finally:
        strip.teardown()


# --------------------------------------------------------------------------- #
# relay
# --------------------------------------------------------------------------- #
def make_relay(**config):
    base = {"pin": 5}
    base.update(config)
    relay = Relay("relay-1", base)
    relay.setup()
    return relay


def test_relay_switching():
    relay = make_relay()
    try:
        assert relay.snapshot()["power"] is False
        assert relay._pin.value() == 0
        relay.apply({"power": True})
        assert relay._pin.value() == 1
        relay.apply({"power": False})
        assert relay._pin.value() == 0
    finally:
        relay.teardown()


def test_relay_active_low():
    relay = make_relay(active_low=True)
    try:
        relay.apply({"power": True})
        assert relay._pin.value() == 0
        relay.apply({"power": False})
        assert relay._pin.value() == 1
    finally:
        relay.teardown()


def test_relay_rejects_bad_auto_off():
    relay = make_relay()
    try:
        with pytest.raises(DeviceError):
            relay.apply({"auto_off_s": -5})
        with pytest.raises(DeviceError):
            relay.apply({"nonsense": True})
    finally:
        relay.teardown()


async def test_relay_auto_off_switches_back():
    relay = make_relay()
    try:
        relay.apply({"auto_off_s": 1, "power": True})
        assert relay.snapshot()["power"] is True
        await asyncio.sleep(1.2)
        assert relay.snapshot()["power"] is False
    finally:
        relay.teardown()


async def test_relay_pulse_returns_to_off():
    relay = make_relay()
    try:
        await relay.action_pulse(ms=60)
        assert relay.snapshot()["power"] is True
        await asyncio.sleep(0.3)
        assert relay.snapshot()["power"] is False
    finally:
        relay.teardown()


# --------------------------------------------------------------------------- #
# RGB strip
# --------------------------------------------------------------------------- #
def test_colour_parsing():
    assert parse_color("#ff8000") == (255, 128, 0)
    assert parse_color("FF8000") == (255, 128, 0)
    assert to_hex(255, 128, 0) == "#ff8000"
    for bad in ("red", "#ff80", "#gggggg", ""):
        with pytest.raises(DeviceError):
            parse_color(bad)


def test_rgb_channels_follow_brightness():
    strip = RgbLedStrip("rgb-1", {"pin_r": 4, "pin_g": 5, "pin_b": 6})
    strip.setup()
    try:
        strip.apply({"power": True, "color": "#ff0000", "brightness": 100, "effect": "solid"})
        assert strip._channels[0].duty_u16() == strip._table[100]
        assert strip._channels[1].duty_u16() == 0
        assert strip._channels[2].duty_u16() == 0
        strip.apply({"brightness": 50})
        assert strip._channels[0].duty_u16() == strip._table[50]
        strip.apply({"power": False})
        assert all(channel.duty_u16() == 0 for channel in strip._channels)
    finally:
        strip.teardown()


def test_rgb_rejects_unknown_effect():
    strip = RgbLedStrip("rgb-1", {})
    strip.setup()
    try:
        with pytest.raises(DeviceError):
            strip.apply({"effect": "disco"})
    finally:
        strip.teardown()


# --------------------------------------------------------------------------- #
# temperature sensor
# --------------------------------------------------------------------------- #
def test_temperature_sensor_simulated_source():
    sensor = TemperatureSensor("temp-1", {"pin": 6, "interval_s": 1})
    sensor.setup()
    values = [sensor._sample() for _ in range(20)]
    assert all(15 < value < 27 for value in values), values
    assert len(set(values)) > 1


def test_temperature_offset_and_validation():
    sensor = TemperatureSensor("temp-1", {"pin": 6})
    sensor.setup()
    assert sensor.snapshot()["offset"] == 0.0
    sensor.apply({"offset": 2.5})
    assert sensor.snapshot()["offset"] == 2.5
    with pytest.raises(DeviceError):
        sensor.apply({"offset": 50})


# --------------------------------------------------------------------------- #
# button
# --------------------------------------------------------------------------- #
def test_button_uses_pull_up_and_inversion():
    button = Button("btn-1", {"pin": 9})
    button.setup()
    try:
        assert button._pin.value() == 1          # pull-up == not pressed
        assert button._read() is False
        button._pin.value(0)                     # pressed to ground
        assert button._read() is True
    finally:
        button.teardown()


def test_button_debounce_window():
    button = Button("btn-1", {"pin": 9, "debounce_ms": 50})
    assert button.debounce_ms == 50
    assert Button("btn-2", {"pin": 9, "debounce_ms": 1}).debounce_ms == 5


# --------------------------------------------------------------------------- #
# base class contract
# --------------------------------------------------------------------------- #
def test_device_requires_uid_and_type():
    class Nameless(Device):
        type = ""

    with pytest.raises(ValueError):
        PwmLedStrip("", {"pin": 4})
    with pytest.raises(ValueError):
        Nameless("x")


def test_set_state_only_reports_changes():
    device = PwmLedStrip("strip-x", {"pin": 4})
    device.set_state({"power": True})
    assert device.snapshot()["power"] is True
    device.set_state({"power": True})  # no change, no error
    assert device.snapshot()["power"] is True
