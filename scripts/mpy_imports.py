"""Import the firmware under a real MicroPython interpreter and exercise the
parts that do not need a live event loop.

This catches portability bugs the CPython test suite cannot: string formatting
on tuples, ``%`` behaviour, integer division, ``b".."`` slicing, ``isinstance``
with multiple types, dict ordering, and so on.

    python scripts/mpy_imports.py
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "micropython"
STUBS = ROOT / "scripts" / "mpy_stubs"

#: Runs on the MicroPython side.  Everything here must be synchronous.
PROBE = r"""
import sys
sys.path.insert(0, __FIRMWARE__)
sys.path.insert(0, __STUBS__)

failures = []

def check(name, fn, *args):
    try:
        result = fn(*args)
    except Exception as exc:
        failures.append((name, '%s: %s' % (type(exc).__name__, exc)))
        return None
    return result

# --- pure helpers -------------------------------------------------------- #
from shnode.transport import parse_url, _b64
check('url: http with port', parse_url, 'http://192.168.1.20:8080/ws/node?node=a&token=b')
ok = parse_url('http://192.168.1.20:8080/ws/node?node=a&token=b')
if ok != ('http', '192.168.1.20', 8080, '/ws/node', 'node=a&token=b'):
    failures.append(('parse_url', repr(ok)))
ok = parse_url('ws://10.0.0.5/ws/node')
if ok != ('ws', '10.0.0.5', 80, '/ws/node', ''):
    failures.append(('parse_url default port', repr(ok)))
ok = parse_url('https://hub.local/x')
if ok != ('https', 'hub.local', 443, '/x', ''):
    failures.append(('parse_url https', repr(ok)))
ok = parse_url('ws://[fe80::1]:9000/a')
if ok != ('ws', 'fe80::1', 9000, '/a', ''):
    failures.append(('parse_url ipv6', repr(ok)))
if _b64(b'\x00\x01ab') != 'AAFhYg==':
    failures.append(('_b64', _b64(b'\x00\x01ab')))

# --- LED strip ----------------------------------------------------------- #
from shnode.devices.pwm_led_strip import PwmLedStrip, gamma_lut
table = gamma_lut(2.2, 65535)
if len(table) != 101 or table[0] != 0 or table[100] != 65535:
    failures.append(('gamma_lut', repr(table[:3]) + '...' + repr(table[-2:])))
strip = PwmLedStrip('strip-1', {'pin': 4, 'gamma': 2.2})
strip.setup()
strip._fade_ms = 0
strip.apply({'power': True})
if strip.snapshot()['power'] is not True:
    failures.append(('strip power', repr(strip.snapshot())))
strip.apply({'brightness': 50})
if strip.snapshot()['brightness'] != 50:
    failures.append(('strip brightness', repr(strip.snapshot())))
strip.apply({'fade_ms': 0, 'frequency_hz': 2000})
if strip.snapshot()['frequency_hz'] != 2000 or strip.snapshot()['fade_ms'] != 0:
    failures.append(('strip frequency', repr(strip.snapshot())))
strip.apply({'power': False})
if strip._target_duty != 0:
    failures.append(('strip off duty', repr(strip._target_duty)))
try:
    strip.apply({'brightness': 250})
    failures.append(('strip range check', 'no error'))
except Exception as exc:
    if 'brightness' not in str(exc):
        failures.append(('strip range message', str(exc)))
try:
    strip.apply({'nonsense': 1})
    failures.append(('strip unknown prop', 'no error'))
except Exception as exc:
    if 'unsupported' not in str(exc):
        failures.append(('strip unknown message', str(exc)))
strip.teardown()

inverted = PwmLedStrip('strip-2', {'pin': 5, 'active_low': True, 'max_duty': 80})
inverted.setup()
inverted._fade_ms = 0
inverted.apply({'power': True, 'brightness': 100})
ceiling = int(65535 * 80 / 100)
if inverted._current_duty != ceiling:
    failures.append(('active_low full', str(inverted._current_duty) + ' != ' + str(ceiling)))
inverted.apply({'power': False})
if inverted._pwm.duty_u16() != ceiling:
    failures.append(('active_low off', str(inverted._pwm.duty_u16())))
inverted.teardown()

# --- relay --------------------------------------------------------------- #
from shnode.devices.relay import Relay
relay = Relay('relay-1', {'pin': 5})
relay.setup()
relay.apply({'power': True})
if relay.snapshot()['power'] is not True:
    failures.append(('relay on', repr(relay.snapshot())))
try:
    relay.apply({'auto_off_s': 30})
    failures.append(('relay auto_off needs a loop', 'no error'))
except RuntimeError:
    pass  # create_task() is stubbed; the real board has a loop
relay.apply({'power': False})
if relay.snapshot()['power'] is not False:
    failures.append(('relay off', repr(relay.snapshot())))
relay.teardown()

low = Relay('relay-2', {'pin': 6, 'active_low': True})
low.setup()
low.apply({'power': True})
if low._pin.value() != 0:
    failures.append(('active low relay', str(low._pin.value())))
low.teardown()

# --- RGB strip ----------------------------------------------------------- #
from shnode.devices.rgb_led_strip import RgbLedStrip, parse_color, to_hex
if parse_color('#FF8000') != (255, 128, 0):
    failures.append(('parse_color', repr(parse_color('#FF8000'))))
if to_hex(255, 128, 0) != '#ff8000':
    failures.append(('to_hex', to_hex(255, 128, 0)))
try:
    parse_color('red')
    failures.append(('parse_color invalid', 'no error'))
except Exception as exc:
    if 'colour' not in str(exc):
        failures.append(('parse_color message', str(exc)))
rgb = RgbLedStrip('rgb-1', {'pin_r': 4, 'pin_g': 5, 'pin_b': 6})
rgb.setup()
rgb.apply({'power': True, 'color': '#00ff00', 'brightness': 100, 'effect': 'solid'})
if rgb.snapshot()['color'] != '#00ff00':
    failures.append(('rgb colour', repr(rgb.snapshot())))
if rgb.snapshot()['effect'] != 'solid':
    failures.append(('rgb effect', repr(rgb.snapshot())))
try:
    rgb.apply({'effect': 'explode'})
    failures.append(('rgb bad effect', 'no error'))
except Exception as exc:
    if 'effect' not in str(exc):
        failures.append(('rgb effect message', str(exc)))
rgb.teardown()

# --- temperature sensor (simulated source) ------------------------------- #
from shnode.devices.temperature_sensor import TemperatureSensor
sensor = TemperatureSensor('temp-1', {'pin': 7, 'interval_s': 5})
sensor.setup()
value = sensor._sample()
if not (-60 < value < 200):
    failures.append(('temp sample', repr(value)))
sensor.apply({'offset': 1.5})
if abs(sensor.snapshot()['offset'] - 1.5) > 1e-9:
    failures.append(('temp offset', repr(sensor.snapshot())))
try:
    sensor.apply({'offset': 99})
    failures.append(('temp offset range', 'no error'))
except Exception as exc:
    if 'offset' not in str(exc):
        failures.append(('temp offset message', str(exc)))

# --- agent (no network) -------------------------------------------------- #
from shnode.agent import Agent
agent = Agent({'server': 'http://127.0.0.1:9', 'node_id': 'probe'})
agent.add(strip)
hello = agent.hello_message()
if hello['t'] != 'hello' or hello['node'] != 'probe':
    failures.append(('hello', repr(hello)))
if not isinstance(hello['devices'], list) or hello['devices'][0]['uid'] != 'strip-1':
    failures.append(('hello devices', repr(hello.get('devices'))))
url = agent.websocket_url()
if 'ws://127.0.0.1:9/ws/node?node=probe' not in url:
    failures.append(('websocket_url', url))
agent.token = 'shd_x'
if 'token=shd_x' not in agent.websocket_url():
    failures.append(('websocket_url token', agent.websocket_url()))

# --- JSON round trip of a protocol message ------------------------------- #
import json
for message in (
    {'t': 'cmd', 'id': 'c1', 'dev': 'strip-1', 'props': {'power': True, 'brightness': 40}},
    {'t': 'ack', 'id': 'c1', 'ok': True, 'state': {'power': True}},
    {'t': 'event', 'dev': 'relay-1', 'event': 'click', 'data': {'held_ms': 120}},
    {'t': 'error', 'code': 'unknown_type', 'detail': 'x'},
):
    if json.loads(json.dumps(message)) != message:
        failures.append(('json roundtrip', repr(message)))

# --- portability guards -------------------------------------------------- #
# Every one of these has bitten us: they exist on CPython but not on a stock
# MicroPython build, so the firmware must not rely on them.
from shnode.compat import random_float, ticks_diff, ticks_ms, write_json, read_json
if not (0.0 <= random_float() < 1.0):
    failures.append(('random_float', 'out of range'))
if not isinstance(ticks_ms(), int):
    failures.append(('ticks_ms', 'not an int'))
if ticks_diff(2, 5) != -3:
    failures.append(('ticks_diff', str(ticks_diff(2, 5))))

if hasattr(random_float, '__call__') and False:
    pass
try:
    import random
    if hasattr(random, 'random'):
        # This particular port ships the helper; most do not, so the firmware
        # uses compat.random_float() instead.  Nothing to do here, but the
        # check documents why the indirection exists.
        pass
except ImportError:
    pass

for module_name in ('urllib', 'typing', 'dataclasses', 'enum', 'itertools',
                    'functools', 'logging', 'statistics', '__future__'):
    try:
        __import__(module_name)
    except ImportError:
        continue
    failures.append(('portability', '%s is available here but absent on most '
                                    'builds - the firmware must not import it' % module_name))

if failures:
    for name, detail in failures:
        print('FAIL', name, '::', detail)
    print('RESULT %d FAILURES' % len(failures))
    raise SystemExit(1)
print('RESULT OK')
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import and exercise the firmware under MicroPython")
    parser.add_argument("--interpreter", default=None)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "scripts"))
    from mpy_check import find_interpreter

    interpreter = find_interpreter(args.interpreter)
    code = PROBE.replace('__FIRMWARE__', repr(str(FIRMWARE))).replace('__STUBS__', repr(str(STUBS)))
    result = subprocess.run([interpreter, "-c", code], capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    print(output)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
