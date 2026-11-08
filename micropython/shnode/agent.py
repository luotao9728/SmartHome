"""The node agent: Wi-Fi, connection management and the protocol loop.

    agent = Agent.from_config("/config.json")
    agent.add(PwmLedStrip("desk-strip", {"pin": 4}))
    agent.run_forever()

What it guarantees
------------------
* reconnects forever with exponential backoff + jitter,
* enrols itself when the hub has its enrolment window open (and persists the
  token it receives),
* applies commands exactly once per id and acks them,
* reports state changes immediately and a full report on a timer,
* answers server pings and sends its own so a dead link is noticed quickly,
* never lets a broken driver take the node offline.
"""

try:  # MicroPython only; CPython runs the same code with Wi-Fi stubbed out
    import network
except ImportError:  # pragma: no cover - desktop
    network = None

from .compat import (
    MICROPYTHON,
    asyncio,
    create_task,
    read_json,
    sleep,
    ticks_diff,
    ticks_ms,
    uptime_s,
    write_json,
)
from .device import Device, DeviceError
from .transport import WebSocketClosed, WebSocketError, WebsocketClient, jitter

FW_VERSION = "1.0.0"
DEFAULT_CONFIG = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "server": "http://192.168.1.10:8080",
    "node_id": "",
    "token": "",
    "name": "",
    "heartbeat_s": 10,
    "report_s": 30,
    "request_timeout_s": 20,
    "static_ip": "",
    "netmask": "",
    "gateway": "",
    "dns": "",
    "ap_fallback": True,
}

MAX_BACKOFF = 60.0

#: Optional hardware watchdog feed function, installed by ``boot.py``.
_WATCHDOG_FEED = None


def set_watchdog(feed) -> None:
    """Install a callable that feeds the hardware watchdog."""
    global _WATCHDOG_FEED
    _WATCHDOG_FEED = feed


def feed_watchdog() -> None:
    if _WATCHDOG_FEED is None:
        return
    try:
        _WATCHDOG_FEED()
    except Exception:  # noqa: BLE001
        pass


def _is_cancel(exc: BaseException) -> bool:
    return type(exc).__name__ in ("CancelledError", "GeneratorExit")


def _task_finished(task) -> bool:
    """``Task.done()`` at import time, but tolerate old MicroPython builds."""
    try:
        return bool(task.done())
    except AttributeError:  # pragma: no cover - very old firmware
        return False


class Agent:
    def __init__(self, config: dict, *, devices=None, verbose: bool = False):
        merged = dict(DEFAULT_CONFIG)
        merged.update(config or {})
        self.config = merged
        self.devices = list(devices or [])
        self.verbose = verbose

        self.node_id = self._node_id()
        self.token = str(merged.get("token") or "")
        self.server = str(merged.get("server") or "").rstrip("/")
        if not self.server:
            raise ValueError("config.server is required (e.g. http://192.168.1.10:8080)")

        self.ws = None
        self.online = False
        self.heartbeat_s = float(merged.get("heartbeat_s") or 10)
        self.report_s = float(merged.get("report_s") or 30)
        self.ack_timeout_s = 3.0

        self._backoff = 1.0
        self._tasks = []
        self._stop = False
        self._report_wanted = set()
        self._last_report = 0.0
        self._handled_ids = []
        self._handled_set = set()
        self._last_rx = 0
        self._boot = ticks_ms()

    # ------------------------------------------------------------------ #
    # construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, path: str = "/config.json", *, devices=None, verbose=False) -> "Agent":
        config = read_json(path, None)
        if config is None:
            raise OSError("cannot read %s" % path)
        agent = cls(config, devices=devices, verbose=verbose)
        agent.config_path = path
        return agent

    def _node_id(self) -> str:
        node_id = str(self.config.get("node_id") or "").strip()
        if node_id:
            return node_id
        if MICROPYTHON:  # pragma: no cover - hardware only
            import machine
            import ubinascii

            node_id = "node-" + ubinascii.hexlify(machine.unique_id()).decode()[-6:]
        else:
            node_id = "node-sim"
        self.config["node_id"] = node_id
        return node_id

    def add(self, device: Device) -> "Agent":
        device.attach(self)
        self.devices.append(device)
        return self

    def device(self, uid: str):
        for device in self.devices:
            if device.uid == uid:
                return device
        return None

    def track(self, task) -> None:
        self._tasks.append(task)

    # ------------------------------------------------------------------ #
    # logging
    # ------------------------------------------------------------------ #
    def log(self, message: str) -> None:
        try:
            print("[%s] %s" % (self.node_id, message))
        except Exception:  # noqa: BLE001
            pass
        if self.verbose:
            try:
                print(message)
            except Exception:  # noqa: BLE001
                pass

    async def send_log(self, message: str) -> None:
        await self._send({"t": "log", "msg": message[:180]})

    # ------------------------------------------------------------------ #
    # network
    # ------------------------------------------------------------------ #
    def connect_wifi(self, *, timeout: float = 30.0) -> bool:
        """Bring up the Wi-Fi station interface.  No-op off-device."""
        if not MICROPYTHON:
            self.log("wifi: skipped (not running on a board)")
            return True
        ssid = str(self.config.get("wifi_ssid") or "")
        password = str(self.config.get("wifi_pass") or "")
        if not ssid:
            self.log("wifi: no ssid configured")
            return False
        import time as _time

        station = network.WLAN(network.STA_IF)
        station.active(True)
        if station.isconnected():
            self.log("wifi: already connected, ip=%s" % (station.ifconfig()[0],))
            return True
        station.connect(ssid, password)

        static_ip = str(self.config.get("static_ip") or "")
        deadline = _time.time() + timeout
        while not station.isconnected() and _time.time() < deadline:
            _time.sleep_ms(250)
        if not station.isconnected():
            self.log("wifi: could not join %r" % ssid)
            return self._start_ap_fallback(station)
        if static_ip:
            try:
                station.ifconfig((
                    static_ip,
                    str(self.config.get("netmask") or "255.255.255.0"),
                    str(self.config.get("gateway") or static_ip.rsplit(".", 1)[0] + ".1"),
                    str(self.config.get("dns") or "1.1.1.1"),
                ))
            except OSError as exc:
                self.log("wifi: static ip failed, keeping dhcp (%s)" % (exc,))
        self.log("wifi: connected, ip=%s" % (station.ifconfig()[0],))
        return True

    def _start_ap_fallback(self, station) -> bool:  # pragma: no cover - hardware
        if not self.config.get("ap_fallback"):
            return False
        try:
            access_point = network.WLAN(network.AP_IF)
            access_point.active(True)
            access_point.config(ssid="SmartHome-%s" % self.node_id[-6:], password="smarthome")
            self.log("wifi: fallback AP %s, open http://192.168.4.1/" % access_point.ifconfig()[0])
        except Exception as exc:  # noqa: BLE001
            self.log("wifi: AP fallback failed: %s" % (exc,))
        return False

    def wifi_rssi(self):
        if not MICROPYTHON:  # pragma: no cover - hardware
            return None
        try:
            station = network.WLAN(network.STA_IF)
            if not station.isconnected():
                return None
            try:
                return int(station.status("rssi"))
            except (ValueError, OSError):
                return None
        except Exception:  # noqa: BLE001
            return None

    def local_ip(self) -> str:
        if not MICROPYTHON:  # pragma: no cover
            return "127.0.0.1"
        try:
            station = network.WLAN(network.STA_IF)
            if station.isconnected():
                return str(station.ifconfig()[0])
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:  # pragma: no cover - hardware entry point
        try:
            asyncio.run(self.main())
        finally:
            try:
                asyncio.new_event_loop()
            except Exception:  # noqa: BLE001
                pass

    async def main(self) -> None:
        await self.start_devices()
        backoff = 1.0
        while not self._stop:
            if not self.connect_wifi():
                self.log("wifi down, retrying in %.0fs" % backoff)
                await sleep(backoff)
                backoff = min(backoff * 1.7, MAX_BACKOFF)
                continue
            try:
                await self.run_session()
                backoff = 1.0
            except _Stopped:
                break
            except Exception as exc:  # noqa: BLE001
                if _is_cancel(exc):
                    break
                self.log("connection lost: %r" % (exc,))
            self.online = False
            delay = jitter(backoff)
            await sleep(delay)
            backoff = min(backoff * 1.8, MAX_BACKOFF)

    async def start_devices(self) -> None:
        for device in self.devices:
            try:
                device.attach(self)
                await device.start()
            except Exception as exc:  # noqa: BLE001
                self.log("device %s failed to start: %r" % (device.uid, exc))

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------ #
    # one connection
    # ------------------------------------------------------------------ #
    def websocket_url(self) -> str:
        base = self.server
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        query = "node=%s" % self.node_id
        if self.token:
            query += "&token=%s" % self.token
        return base + "/ws/node?" + query

    async def run_session(self) -> None:
        self.log("connecting to %s" % self.server)
        self.ws = await WebsocketClient.connect(
            self.websocket_url(),
            timeout=float(self.config.get("request_timeout_s") or 20),
        )
        tasks = []
        try:
            await self._handshake()
            self.online = True
            self._last_rx = ticks_ms()
            tasks.append(create_task(self._read_loop()))
            tasks.append(create_task(self._heartbeat_loop()))
            tasks.append(create_task(self._report_loop()))
            tasks.append(create_task(self._report_flusher()))
            self.log("online (%d devices)" % len(self.devices))
            # Report everything once connected so the hub is in sync.
            await self.report_all()
            while not self._stop:
                await sleep(0.5)
                feed_watchdog()
                for task in tasks:
                    if _task_finished(task):
                        # MicroPython's Task has no .exception(): the failure is
                        # surfaced by the task itself, so a finished task is
                        # reason enough to drop the connection and reconnect.
                        raise WebSocketClosed("connection task ended")
        finally:
            self.online = False
            for task in tasks:
                task.cancel()
            ws = self.ws
            self.ws = None
            if ws is not None:
                await ws.close()

    async def _handshake(self) -> None:
        # The hub may first send "enrolled" with a token, then it waits for our
        # hello.  Send hello immediately and read the reply with a timeout.
        await self._send(self.hello_message())
        deadline = ticks_ms() + int(float(self.config.get("request_timeout_s") or 20) * 1000)
        while True:
            remaining = ticks_diff(deadline, ticks_ms()) / 1000.0
            if remaining <= 0:
                raise WebSocketClosed("handshake timeout")
            try:
                message = await asyncio.wait_for(self.ws.receive_json(), remaining)
            except asyncio.TimeoutError:
                raise WebSocketClosed("handshake timeout")
            kind = message.get("t") if isinstance(message, dict) else None
            if kind == "enrolled":
                self.token = str(message.get("token") or "")
                self._persist_token()
                self.log("enrolled, token stored")
                continue
            if kind == "error":
                raise WebSocketError("server refused: %s" % (message.get("detail") or message.get("code")))
            if kind == "hello":
                self.heartbeat_s = float(message.get("heartbeat_s") or self.heartbeat_s)
                self.report_s = float(message.get("report_s") or self.report_s)
                self.ack_timeout_s = float(message.get("ack_timeout_s") or self.ack_timeout_s)
                return
            # Anything else before hello is unexpected but harmless.
            self.log("ignoring pre-hello message %r" % (kind,))

    def _persist_token(self) -> None:
        path = getattr(self, "config_path", None)
        if not path:
            return
        stored = read_json(path, {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        stored["token"] = self.token
        stored["node_id"] = self.node_id
        write_json(path, stored)

    def hello_message(self) -> dict:
        return {
            "t": "hello",
            "node": self.node_id,
            "board": self._board_name(),
            "fw": FW_VERSION,
            "ip": self.local_ip(),
            "rssi": self.wifi_rssi(),
            "uptime": round(uptime_s(), 1),
            "devices": [self._device_hello(device) for device in self.devices],
        }

    def _device_hello(self, device: Device) -> dict:
        payload = {"uid": device.uid, "type": device.type, "name": device.name, "state": device.snapshot()}
        extra = device.describe()
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _board_name() -> str:
        if not MICROPYTHON:  # pragma: no cover
            return "cpython"
        try:
            import sys

            return str(sys.platform)
        except Exception:  # noqa: BLE001
            return "unknown"

    # ------------------------------------------------------------------ #
    # loops
    # ------------------------------------------------------------------ #
    async def _read_loop(self) -> None:
        while not self._stop:
            if self.ws is None:
                return
            timeout = max(5.0, self.heartbeat_s * 3.0)
            try:
                message = await asyncio.wait_for(self.ws.receive_json(), timeout)
            except asyncio.TimeoutError:
                self.log("nothing from hub for %.0fs" % timeout)
                return
            self._last_rx = ticks_ms()
            if not isinstance(message, dict):
                continue
            await self._handle(message)

    async def _handle(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "cmd":
            await self._handle_cmd(message)
        elif kind == "act":
            await self._handle_act(message)
        elif kind == "ping":
            await self._send({"t": "pong", "n": message.get("n")})
        elif kind == "pong":
            pass
        elif kind == "state_req":
            await self.report_all()
        elif kind == "config":
            self.heartbeat_s = float(message.get("heartbeat_s") or self.heartbeat_s)
            self.report_s = float(message.get("report_s") or self.report_s)
            self.log("config updated: heartbeat=%.0fs report=%.0fs" % (self.heartbeat_s, self.report_s))
        elif kind == "bye":
            self.log("hub is restarting")
            raise WebSocketClosed("hub restart")
        elif kind == "enrolled":
            self.token = str(message.get("token") or self.token)
            self._persist_token()
        elif kind == "hello":
            self.ack_timeout_s = float(message.get("ack_timeout_s") or self.ack_timeout_s)
        elif kind == "error":
            self.log("server error: %s" % (message.get("detail") or message.get("code")))
        else:
            self.log("unknown message %r" % (kind,))

    async def _handle_cmd(self, message: dict) -> None:
        msg_id = message.get("id")
        uid = message.get("dev")
        props = message.get("props") or {}
        if not isinstance(msg_id, str) or not isinstance(props, dict):
            return
        if msg_id in self._handled_set:
            return  # duplicate delivery after a retry: already applied
        device = self.device(uid)
        if device is None:
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": "unknown device %s" % uid})
            return
        try:
            settable = getattr(device, "settable", None) or ()
            clean = {key: value for key, value in props.items() if key in settable}
            if not clean:
                raise DeviceError("nothing to apply for %s" % (sorted(props),))
            device.apply(clean)
            self._remember(msg_id)
            await self._send({"t": "ack", "id": msg_id, "ok": True, "state": device.snapshot()})
        except DeviceError as exc:
            self._remember(msg_id)
            self.log("%s rejected %r: %s" % (uid, props, exc))
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._remember(msg_id)
            self.log("%s failed %r: %r" % (uid, props, exc))
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": repr(exc)})

    async def _handle_act(self, message: dict) -> None:
        msg_id = message.get("id")
        uid = message.get("dev")
        action = message.get("action")
        args = message.get("args") or {}
        device = self.device(uid)
        if device is None:
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": "unknown device"})
            return
        handler = getattr(device, "action_" + str(action), None)
        if handler is None:
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": "unknown action %r" % (action,)})
            return
        try:
            result = handler(**args) if args else handler()
            if hasattr(result, "__await__"):
                await result
            await self._send({"t": "ack", "id": msg_id, "ok": True, "state": device.snapshot()})
        except DeviceError as exc:
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self.log("action %s failed: %r" % (action, exc))
            await self._send({"t": "ack", "id": msg_id, "ok": False, "error": repr(exc)})

    def _remember(self, msg_id: str) -> None:
        # Bounded memory: the hub only retries while a command is pending.
        self._handled_ids.append(msg_id)
        self._handled_set.add(msg_id)
        if len(self._handled_ids) > 64:
            old = self._handled_ids.pop(0)
            self._handled_set.discard(old)

    async def _heartbeat_loop(self) -> None:
        while not self._stop:
            await sleep(max(1.0, self.heartbeat_s))
            await self._send({"t": "ping", "n": int(ticks_ms() / 1000) % 100000})
            await self._report_pending()

    async def _report_loop(self) -> None:
        while not self._stop:
            interval = max(1.0, float(self.report_s))
            await sleep(interval)
            await self.report_all()

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #
    def request_report(self, device: Device) -> None:
        """Called by devices when their state changed (synchronous).

        The actual send happens in :meth:`_report_flusher`, so a device can call
        this from anywhere (including inside a command handler) without
        spawning a task per change.
        """
        self._report_wanted.add(device.uid)

    async def _report_flusher(self) -> None:
        """Coalesce state changes and send them in small batches.

        Rapid changes (a slider drag, a fade) collapse into one report per
        interval, which keeps the Wi-Fi link quiet without adding latency the
        user can notice.
        """
        while True:
            await sleep(0.05)
            if not self._report_wanted or not self.online:
                continue
            await self._report_pending()

    async def _report_pending(self) -> None:
        while self._report_wanted:
            uid = self._report_wanted.pop()
            device = self.device(uid)
            if device is None:
                continue
            await self._send({"t": "state", "dev": uid, "values": device.snapshot()})

    async def report_all(self) -> None:
        for device in self.devices:
            self._report_wanted.discard(device.uid)
            await self._send({"t": "state", "dev": device.uid, "values": device.snapshot()})
        self._last_report = ticks_ms()

    async def event(self, device: Device, name: str, data=None) -> None:
        await self._send({
            "t": "event",
            "dev": device.uid if device else "",
            "event": name,
            "data": data or {},
        })

    # ------------------------------------------------------------------ #
    # low level send
    # ------------------------------------------------------------------ #
    async def _send(self, message: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except (WebSocketClosed, OSError) as exc:
            self.log("send failed: %r" % (exc,))
            self.online = False
            return False


class _Stopped(Exception):
    pass
