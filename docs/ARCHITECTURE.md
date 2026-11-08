# Architecture

A short tour of how the pieces fit, and why the seams are where they are.

## The two-state model

Every device has two property maps in the hub's database:

* **desired** – what the user asked for.  Written the instant a command
  arrives, persisted, and never touched by the hardware.
* **reported** – what the device last told us.  Only ever written from a node
  message (`ack` or `state`).

A property is "in sync" when the two agree.  Everything else in the hub follows
from that:

```
user drags slider ──► desired.brightness = 40 ──► cmd ──► node applies
                                                        │
                              ack{state} ◄──────────────┘
                                    │
                                    ▼
                            reported.brightness = 40  → UI shows synced
```

If the node reboots, loses Wi-Fi, or reports something different, the hub
notices at the next handshake and re-sends the difference
(`Hub._reconcile_node`).  There is no command queue to lose: the difference
*is* the queue.

## Connection lifecycle

```
node                    hub
 │  ws connect?node/token │
 │───────────────────────►│  authenticate (token, or enrolment window)
 │◄───────────────────────│  (optional) enrolled {token}
 │  hello {devices, state}│
 │───────────────────────►│  register/update devices, adopt reported state
 │◄───────────────────────│  hello {heartbeat_s, report_s, ack_timeout_s}
 │◄───────────────────────│  cmd {...} for anything out of sync
 │  ack {state}           │
 │───────────────────────►│
 │  state {dev, values}   │  whenever the hardware changes on its own
 │───────────────────────►│
 │        ping/pong       │  both directions, every heartbeat_s
```

A second connection with the same node id replaces the first (the old socket
gets close code 4000), so a crashed board never leaves a ghost session.

## Why ack + retry instead of fire-and-forget

Wi-Fi is lossy and microcontrollers reboot.  A command that is silently dropped
is indistinguishable from one that worked — until the user notices the light is
still on.  So:

1. `cmd` carries an id.
2. The node applies it, records the id, and answers `ack`.
3. No ack within `ack_timeout_s` → resend, up to `ack_retries` times.
4. Still nothing → the card shows an error and the desired value stays queued.

The node keeps a small ring of recent ids so a retried command is applied
**once** (`Agent._remember`).

## Why one WebSocket and not MQTT

MQTT would add a broker to run, secure and back up, and the state model would
still have to be built on top (retained messages are a poor fit for
acknowledged commands).  A single WebSocket keeps the deployment to "one
Python process", keeps the protocol readable in a browser devtools window, and
lets the node and hub exchange JSON that both sides already validate.

The trade-off: the hub must implement the reliability layer itself — which is
what `hub.py` and `agent.py` do, and what `tests/test_hub.py` and
`tests/test_e2e.py` verify.

## Device types are data

A type manifest declares properties (with ranges and UI hints), the hardware
config schema, actions and events.  Three consumers read it:

| Consumer | Uses it for |
| --- | --- |
| `registry.py` | validating manifests, coercing values, validating actions |
| `app.js` | choosing and labelling a control for each property |
| `main.py` (node) | nothing — the driver declares its own `settable` tuple |

Adding a type therefore touches exactly two files (manifest + driver) and one
line of registration in `main.py`.  No server route, no UI code, no migration.

## Persistence

SQLite in WAL mode.  Slider drags would otherwise write on every pixel, so the
hub batches `reported` writes: devices are marked dirty and flushed every two
seconds and on disconnect (`Hub._flush_dirty`).  History is sampled (sensors
always, everything else at most every 5 s) and pruned daily.

The config file is written atomically (`os.replace`) because a power cut
mid-write would otherwise leave the hub unable to start.

## Security boundaries

* The browser talks to the hub with a signed, `HttpOnly` session cookie; API
  scripts can use an API key instead.
* A node cannot read or write anything but its own devices: the token binds it
  to a node id, and every message is checked against the session.
* Tokens are stored as SHA-256 hashes, so a copy of the database is not a set
  of working credentials.
* The hub never executes anything a node sends; property values are coerced
  against the manifest before they reach the database or the UI.

## Testing strategy

| Layer | File | What it proves |
| --- | --- | --- |
| Protocol | `test_protocol.py` | coercion, ranges, unknown fields, message shapes |
| Security | `test_security.py` | hashing, cookies, tampering, expiry |
| Registry | `test_registry.py` | manifests load, bad ones are reported not fatal |
| Hub | `test_hub.py` | registration, ack/retry, reconcile, reconnect, watchdog |
| API | `test_server.py` | auth on every route, CRUD, validation, WebSockets |
| Drivers | `test_firmware_drivers.py` | gamma, fading, inversion, clamping, actions |
| End to end | `test_e2e.py` | the real firmware against a real uvicorn server |
| UI | `test_ui.py` | a real browser: cards, toggles, dialogs, no console errors |
| Portability | `scripts/mpy_check.py`, `scripts/mpy_imports.py` | the firmware compiles and runs under real MicroPython |
