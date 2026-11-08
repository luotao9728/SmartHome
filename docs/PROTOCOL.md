# Node protocol reference

Version 1.  One JSON object per WebSocket **text** frame on
`/ws/node?node=<id>&token=<token>`.  Frames larger than 64 KiB are rejected.

General rules:

* Unknown message types are logged and ignored by the hub; unknown message
  types from the hub are logged and ignored by the node.
* Unknown properties are dropped, not fatal, when a node *reports* state.
  They are an error when a *command* asks for them.
* All numbers are JSON numbers.  Timestamps from the hub are seconds since the
  epoch (float); `ping`/`pong` counters are small integers.

## Node → hub

### `hello`

Must be the first frame.  Re-sending it later is allowed and re-registers
devices (useful if a board adds hardware at runtime).

```json
{
  "t": "hello",
  "node": "esp32-desk",
  "board": "esp32",
  "fw": "1.0.0",
  "ip": "192.168.1.42",
  "rssi": -58,
  "uptime": 1234.5,
  "devices": [
    {
      "uid": "desk-strip",
      "type": "pwm_led_strip",
      "name": "Desk strip",
      "config": {"pin": 4},
      "state": {"power": true, "brightness": 40}
    }
  ]
}
```

`uid` must match `[A-Za-z0-9_.-]{1,64}` and be unique across the whole hub.
`type` must exist in the hub's manifests, otherwise the device is skipped and
the node receives an `error` message.

### `state`

Full snapshot for one device.  Sent after every local change and periodically.

```json
{"t": "state", "dev": "desk-strip", "values": {"power": true, "brightness": 40}}
```

### `ack`

Answer to `cmd` or `act`.

```json
{"t": "ack", "id": "c12", "ok": true, "state": {"power": true, "brightness": 40}}
{"t": "ack", "id": "c13", "ok": false, "error": "pin 4 is busy"}
```

`id` must be echoed exactly.  The hub ignores acks for unknown or foreign ids.

### `event`

Something happened that is not a property (a button, a threshold, a fault).

```json
{"t": "event", "dev": "desk-button", "event": "click", "data": {}}
```

### `ping` / `pong`

```json
{"t": "ping", "n": 17}
{"t": "pong", "n": 17}
```

The hub answers every `ping` and measures RTT on its own pings.

### `log`

Free-form, rate-limited by convention; written to the hub log.

```json
{"t": "log", "msg": "wifi reconnected"}
```

## Hub → node

### `hello`

```json
{"t": "hello", "v": 1, "node": "esp32-desk", "time": 1712345678.5,
 "heartbeat_s": 10, "report_s": 30, "ack_timeout_s": 3.0}
```

The node adopts the timing values.

### `cmd`

```json
{"t": "cmd", "id": "c12", "dev": "desk-strip", "props": {"brightness": 40}, "ts": 1712345678.5}
```

### `act`

```json
{"t": "act", "id": "a3", "dev": "desk-strip", "action": "identify", "args": {}}
```

### `state_req`

Ask the node to report every device now (sent after a UI refresh).

```json
{"t": "state_req", "id": "s7"}
```

### `config`

Timing changed in the UI.

```json
{"t": "config", "heartbeat_s": 15, "report_s": 60, "ack_timeout_s": 4.0}
```

### `ping`

```json
{"t": "ping", "n": 4, "ts": 1712345678.5}
```

### `bye`

The hub is shutting down; reconnect after `retry_s`.

```json
{"t": "bye", "retry_s": 5}
```

### `enrolled`

Sent before `hello` when the node authenticated through the enrolment window.

```json
{"t": "enrolled", "node": "esp32-new", "token": "shd_...", "server": "SmartHome"}
```

### `error`

```json
{"t": "error", "code": "unknown_type", "detail": "my_thing", "dev": "x"}
{"t": "error", "code": "unauthorized", "detail": "invalid token"}
```

## Close codes

| Code | Meaning |
| --- | --- |
| 1000 | normal |
| 4000 | another connection with the same node id took over |
| 4001 | hub is shutting down |
| 4002 | bad/missing `hello` (or token/node mismatch) |
| 4008 | heartbeat timeout |
| 4401 | invalid or missing token |

The node treats every close as "reconnect with backoff", except when it has been
told its token is invalid (4401) — that needs a human.

## Timing defaults

| Parameter | Default | Notes |
| --- | --- | --- |
| `heartbeat_s` | 10 | hub pings at this interval; node pings too |
| `report_s` | 30 | full state report |
| `offline_after_s` | 40 | silence before the hub drops the session (must be ≥ 2 × heartbeat) |
| `ack_timeout_s` | 3.0 | per attempt |
| `ack_retries` | 2 | retries after the first attempt |

The node also declares a 50 ms coalescing window for state changes, so a fade or
a slider drag produces a handful of frames rather than hundreds.
