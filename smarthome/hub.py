"""The hub: device state, node connections and command delivery.

Design notes
------------
* **One connection per node.**  A node is a microcontroller that owns one or
  more devices.  When the same node id connects twice the older socket is
  replaced, so a crashed ESP32 never leaves a ghost session behind.
* **Desired vs reported state.**  ``desired`` is what the user asked for and is
  persisted immediately; ``reported`` is what the hardware says.  Any
  difference is reconciled whenever the node connects, so a reboot or a Wi-Fi
  outage converges back to the user's intent.
* **Commands are acknowledged.**  A ``cmd`` carries an id; the node answers
  with ``ack``.  The hub retries with a timeout and finally records an error
  instead of silently dropping the command.
* **Everything is asynchronous.**  Each node session owns a heartbeat task and
  a watchdog; the hub itself has a persistence task so SD cards are not worn
  out by slider movements.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set

from . import protocol
from .config import Config
from .registry import ManifestError, TypeRegistry
from .store import Store

LOG = logging.getLogger(__name__)

TRANSPORT_REPLACED = 4000
TRANSPORT_SHUTDOWN = 4001
TRANSPORT_BAD_HELLO = 4002
REPLY_ACTION_OK = "ok"

#: Sanity limits so one misbehaving board cannot exhaust the hub's memory.
MAX_DEVICES_PER_NODE = 64
MAX_PROPERTY_KEYS = 128


# --------------------------------------------------------------------------- #
# Transport abstraction (implemented by the FastAPI adapter and by tests)
# --------------------------------------------------------------------------- #
class NodeTransport(Protocol):
    remote: str

    async def recv(self) -> Optional[Dict[str, Any]]:
        ...

    async def send(self, message: Dict[str, Any]) -> None:
        ...

    async def close(self, code: int = 1000, reason: str = "") -> None:
        ...

    @property
    def connected(self) -> bool:
        ...


@dataclass
class PendingCommand:
    msg_id: str
    uid: str
    node_id: str
    props: Dict[str, Any]
    created: float
    attempts: int = 0
    future: Optional[asyncio.Future] = None


@dataclass
class NodeSession:
    node_id: str
    transport: NodeTransport
    connected_at: float
    last_rx: float
    first_seen: float
    board: str = ""
    fw: str = ""
    ip: str = ""
    rssi: Optional[int] = None
    uptime: Optional[float] = None
    ping_seq: int = 0
    pending_pings: Dict[int, float] = field(default_factory=dict)
    rtt_ms: Optional[float] = None
    devices: Set[str] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    watchdog: Optional[asyncio.Task] = None

    @property
    def online(self) -> bool:
        return self.transport.connected


class DeviceError(RuntimeError):
    """User visible error while manipulating a device."""


class UnknownDevice(DeviceError):
    pass


class OfflineNode(DeviceError):
    pass


#: Sentinel for "leave this field alone" in partial updates.  ``None`` cannot be
#: used because clearing ``node_id`` is a legitimate edit.
UNSET: Any = object()


# --------------------------------------------------------------------------- #
# The hub
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self, config: Config, store: Store, registry: TypeRegistry):
        self.config = config
        self.store = store
        self.registry = registry
        self.started_at = time.time()

        self._sessions: Dict[str, NodeSession] = {}
        self._pending: Dict[str, PendingCommand] = {}
        self._pending_by_device: Dict[str, Set[str]] = {}
        self._device_errors: Dict[str, str] = {}
        self._dirty: Set[str] = set()
        self._ui_clients: Set[asyncio.Queue] = set()
        self._msg_seq = itertools.count(1)
        self._flush_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None
        self._closing = False
        # Created lazily: on Python 3.9 an asyncio primitive binds the running
        # loop at construction time, and the hub is often built before (or
        # outside) the loop that will run it.
        self._lock: Optional[asyncio.Lock] = None
        self._hist_ts: Dict[str, float] = {}

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.registry.reload()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="hub-flush")
        self._prune_task = asyncio.create_task(self._prune_loop(), name="hub-prune")

    async def stop(self) -> None:
        self._closing = True
        for task in filter(None, (self._flush_task, self._prune_task)):
            task.cancel()
        for session in list(self._sessions.values()):
            try:
                await session.transport.send({"t": protocol.BYE, "retry_s": 5})
            except Exception:  # pragma: no cover - best effort
                pass
            await self._close_session(session, TRANSPORT_SHUTDOWN, "server shutting down")
        self._flush_dirty()
        for queue in list(self._ui_clients):
            queue.put_nowait({"t": "bye"})

    # ------------------------------------------------------------------ #
    # serialisation helpers
    # ------------------------------------------------------------------ #
    def _device_public(self, device: Dict[str, Any]) -> Dict[str, Any]:
        dtype = self.registry.get(device["type"])
        session = self._sessions.get(device.get("node_id") or "")
        pending = sorted(
            self._pending_by_device.get(device["uid"], set())
        )
        reported = dict(device.get("reported") or {})
        desired = dict(device.get("desired") or {})
        if dtype is not None:
            # Never publish a read-only property as something the user wants.
            # (Databases written by older versions may still contain them.)
            for key, spec in dtype.properties.items():
                if not spec.get("settable", True):
                    desired.pop(key, None)
        node_online = bool(session and session.online)
        # A device is only reachable if its node is connected *and* that node
        # actually announced it -- a device created in the UI for a board that
        # has not been flashed yet is not "online" just because another device
        # on the same board is.
        announced = bool(session and device["uid"] in session.devices)
        return {
            "uid": device["uid"],
            "name": device["name"],
            "room": device.get("room", ""),
            "type": device["type"],
            "type_name": dtype.name if dtype else device["type"],
            "category": dtype.category if dtype else "other",
            "icon": dtype.icon if dtype else "generic",
            "node_id": device.get("node_id"),
            "node_online": node_online,
            "announced": announced,
            "online": node_online and announced,
            "unreachable": node_online and not announced,
            "config": device.get("config") or {},
            "desired": desired,
            "reported": reported,
            "pending": pending,
            "error": self._device_errors.get(device["uid"], ""),
            "sort_index": device.get("sort_index", 0),
            "updated": device.get("updated"),
        }

    def _node_public(self, session: NodeSession) -> Dict[str, Any]:
        devices = self.store.devices_for_node(session.node_id)
        return {
            "node_id": session.node_id,
            "online": session.online,
            "board": session.board,
            "fw": session.fw,
            "ip": session.ip,
            "rssi": session.rssi,
            "uptime": session.uptime,
            "rtt_ms": session.rtt_ms,
            "connected_at": session.connected_at,
            "last_seen": session.last_rx,
            "device_count": len(devices),
        }

    def snapshot(self) -> Dict[str, Any]:
        devices = self.store.list_devices()
        stored_nodes = {row["node_id"]: row for row in self.store.list_nodes()}
        nodes: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for session in self._sessions.values():
            nodes.append(self._node_public(session))
            seen.add(session.node_id)
        for node_id, row in stored_nodes.items():
            if node_id in seen:
                continue
            nodes.append(
                {
                    "node_id": node_id,
                    "online": False,
                    "board": row.get("board", ""),
                    "fw": row.get("fw", ""),
                    "ip": row.get("ip", ""),
                    "rssi": row.get("rssi"),
                    "uptime": None,
                    "rtt_ms": None,
                    "connected_at": None,
                    "last_seen": row.get("last_seen"),
                    "device_count": len(self.store.devices_for_node(node_id)),
                }
            )
        nodes.sort(key=lambda n: n["node_id"])
        return {
            "t": "snapshot",
            "devices": [self._device_public(d) for d in devices],
            "nodes": nodes,
            "types": self.registry.public(),
            "type_errors": self.registry.errors,
            "settings": self._public_settings(),
            "server_time": time.time(),
            "version": self.config.data.get("version", 1),
        }

    def _public_settings(self) -> Dict[str, Any]:
        dev = self.config.devices
        return {
            "heartbeat_s": dev["heartbeat_s"],
            "report_s": dev["report_s"],
            "offline_after_s": dev["offline_after_s"],
            "ack_timeout_s": dev["ack_timeout_s"],
            "ack_retries": dev["ack_retries"],
            "history_days": dev["history_days"],
            "title": self.config.ui.get("title", "SmartHome"),
            "theme": self.config.ui.get("theme", "auto"),
            "device_types_dirs": [str(p) for p in self.config.device_types_dirs],
        }

    # ------------------------------------------------------------------ #
    # UI broadcast
    # ------------------------------------------------------------------ #
    def register_ui(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._ui_clients.add(queue)
        return queue

    def unregister_ui(self, queue: asyncio.Queue) -> None:
        self._ui_clients.discard(queue)

    def broadcast(self, message: Dict[str, Any]) -> None:
        if not self._ui_clients:
            return
        for queue in list(self._ui_clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow browser: drop it and let it resync on reconnect.
                self._ui_clients.discard(queue)

    def _broadcast_device(self, uid: str) -> None:
        device = self.store.get_device(uid)
        if device is not None:
            self.broadcast({"t": "device", "device": self._device_public(device)})

    def _broadcast_nodes(self) -> None:
        for node in self.snapshot()["nodes"]:
            self.broadcast({"t": "node", "node": node})

    # ------------------------------------------------------------------ #
    # device CRUD
    # ------------------------------------------------------------------ #
    def list_devices(self) -> List[Dict[str, Any]]:
        return [self._device_public(d) for d in self.store.list_devices()]

    def get_device(self, uid: str) -> Dict[str, Any]:
        device = self.store.get_device(uid)
        if device is None:
            raise UnknownDevice("unknown device %r" % (uid,))
        return device

    def create_device(
        self,
        *,
        uid: Optional[str] = None,
        type_id: str,
        name: Optional[str] = None,
        room: str = "",
        node_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        desired: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        dtype = self.registry.get(type_id)
        if dtype is None:
            raise DeviceError("unknown device type %r" % (type_id,))
        uid = (uid or self._new_uid(dtype.id)).strip()
        if not self._valid_uid(uid):
            raise DeviceError("invalid device id %r" % (uid,))
        if self.store.get_device(uid) is not None:
            raise DeviceError("device %r already exists" % (uid,))
        if node_id is not None and self.store.get_node(node_id) is None:
            raise DeviceError("unknown node %r" % (node_id,))
        try:
            clean_config = dtype.validate_config(config or {})
            props, errors = protocol.normalize_props(dtype, desired or {}, only_settable=True)
        except ManifestError as exc:
            raise DeviceError(str(exc))
        if errors:
            raise DeviceError("; ".join(errors))
        merged_desired = dtype.default_props()
        merged_desired.update(props)
        merged_config = dict(clean_config)
        if not merged_config:
            merged_config = dtype.default_config()
        record = {
            "uid": uid,
            "type": dtype.id,
            "name": (name or uid).strip() or uid,
            "room": room,
            "node_id": node_id,
            "config": merged_config,
            "desired": merged_desired,
            "reported": {},
            "sort_index": self.store.next_sort_index(),
        }
        self.store.insert_device(record)
        self.store.add_event("device_created", uid=uid, name=record["name"])
        self._broadcast_device(uid)
        self.broadcast({"t": "event", "event": self._event_public(self.store.list_events(limit=1)[0])})
        return self._device_public(self.store.get_device(uid))

    def update_device(self, uid: str, *, name: Optional[str] = None, room: Optional[str] = None,
                      node_id: Any = UNSET, config: Optional[Dict[str, Any]] = None,
                      sort_index: Optional[int] = None) -> Dict[str, Any]:
        device = self.get_device(uid)
        dtype = self.registry.get(device["type"])
        fields: Dict[str, Any] = {}
        if name is not None:
            fields["name"] = name.strip() or device["name"]
        if room is not None:
            fields["room"] = room
        if node_id is not UNSET:
            # ``None`` or "" unassigns the device (it keeps working, the hub
            # just no longer expects a particular board to report it).
            if node_id and self.store.get_node(node_id) is None:
                raise DeviceError("unknown node %r" % (node_id,))
            fields["node_id"] = node_id or None
        if config is not None:
            if dtype is None:
                raise DeviceError("device type %r is no longer available" % (device["type"],))
            try:
                fields["config"] = dtype.validate_config(config)
            except ManifestError as exc:
                raise DeviceError(str(exc))
        if sort_index is not None:
            fields["sort_index"] = int(sort_index)
        if not fields:
            return self._device_public(device)
        self.store.update_device(uid, **fields)
        self.store.add_event("device_updated", uid=uid, name=fields.get("name", device["name"]))
        # A changed hardware config may need to be re-sent to the node.
        self._broadcast_device(uid)
        return self._device_public(self.store.get_device(uid))

    def delete_device(self, uid: str, *, also_remove: bool = True) -> None:
        device = self.get_device(uid)
        self.store.delete_device(uid)
        self._dirty.discard(uid)
        self._pending_by_device.pop(uid, None)
        self._device_errors.pop(uid, None)
        self.store.add_event("device_deleted", uid=uid, name=device["name"])
        self.broadcast({"t": "removed", "uid": uid})
        self._broadcast_nodes()

    # ------------------------------------------------------------------ #
    # property updates and actions
    # ------------------------------------------------------------------ #
    async def set_props(self, uid: str, values: Dict[str, Any], *, source: str = "user") -> Dict[str, Any]:
        device = self.get_device(uid)
        dtype = self.registry.get(device["type"])
        if dtype is None:
            raise DeviceError("device type %r is not available" % (device["type"],))
        clean, errors = protocol.normalize_props(dtype, values or {}, only_settable=True)
        if errors:
            raise DeviceError("; ".join(errors))
        if not clean:
            raise DeviceError("no settable properties supplied")

        desired = dict(device.get("desired") or {})
        changed = {k: v for k, v in clean.items() if not protocol.values_equal(desired.get(k), v)}
        if not changed:
            return self._device_public(device)
        desired.update(changed)
        self.store.update_device(uid, desired=desired)
        self._dirty.add(uid)
        self._broadcast_device(uid)
        session = self._sessions.get(device.get("node_id") or "")
        if session is None or not session.online:
            self._device_errors[uid] = "node offline - the command is queued"
            self._broadcast_device(uid)
            return self._device_public(self.store.get_device(uid))

        self._device_errors.pop(uid, None)
        await self._dispatch(session, uid, changed)
        return self._device_public(self.store.get_device(uid))

    async def run_action(self, uid: str, action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        device = self.get_device(uid)
        dtype = self.registry.get(device["type"])
        if dtype is None:
            raise DeviceError("device type %r is not available" % (device["type"],))
        try:
            clean_args = dtype.validate_args(action, args or {})
        except ManifestError as exc:
            raise DeviceError(str(exc))
        session = self._sessions.get(device.get("node_id") or "")
        if session is None or not session.online:
            raise OfflineNode("node is offline")
        msg_id = self._next_id("a")
        await self._send(
            session,
            protocol.act(msg_id, uid, action, clean_args),
        )
        self.store.add_event("action", uid=uid, name=device["name"], data={"action": action, "args": clean_args})
        self._broadcast_event(self.store.list_events(limit=1)[0])
        return {"ok": True, "action": action, "args": clean_args}

    async def _dispatch(self, session: NodeSession, uid: str, props: Dict[str, Any]) -> bool:
        msg_id = self._next_id("c")
        pending = PendingCommand(
            msg_id=msg_id,
            uid=uid,
            node_id=session.node_id,
            props=dict(props),
            created=time.time(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[msg_id] = pending
        self._pending_by_device.setdefault(uid, set()).add(msg_id)
        self._broadcast_device(uid)
        message = protocol.cmd(msg_id, uid, props, ts=time.time())
        asyncio.create_task(self._drive_command(session, pending, message), name="cmd-%s" % msg_id)
        return True

    async def _drive_command(self, session: NodeSession, pending: PendingCommand,
                             message: Dict[str, Any]) -> None:
        cfg = self.config.devices
        timeout = float(cfg["ack_timeout_s"])
        retries = int(cfg["ack_retries"])
        try:
            for attempt in range(retries + 1):
                if not session.online:
                    raise OfflineNode("node went offline")
                pending.attempts = attempt + 1
                await self._send(session, message)
                try:
                    await asyncio.wait_for(asyncio.shield(pending.future), timeout)
                    return
                except asyncio.TimeoutError:
                    LOG.warning("no ack for %s (%s) attempt %d", pending.uid, pending.msg_id, attempt + 1)
                    continue
            raise TimeoutError("no acknowledgement from node")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            LOG.warning("command %s for %s failed: %s", pending.msg_id, pending.uid, exc)
            self._device_errors[pending.uid] = str(exc) or "command failed"
            self.broadcast(
                {
                    "t": "toast",
                    "level": "error",
                    "text": "%s: %s" % (pending.uid, self._device_errors[pending.uid]),
                }
            )
        finally:
            self._pending.pop(pending.msg_id, None)
            self._pending_by_device.get(pending.uid, set()).discard(pending.msg_id)
            self._broadcast_device(pending.uid)

    async def _send(self, session: NodeSession, message: Dict[str, Any]) -> None:
        try:
            await session.transport.send(message)
        except Exception as exc:  # noqa: BLE001
            raise OfflineNode("send failed: %s" % (exc,))

    # ------------------------------------------------------------------ #
    # node sessions
    # ------------------------------------------------------------------ #
    def _next_id(self, prefix: str) -> str:
        return "%s%d" % (prefix, next(self._msg_seq))

    async def serve_node(self, transport: NodeTransport, *, token_node_id: Optional[str] = None) -> None:
        """Run one node connection until it closes."""
        session: Optional[NodeSession] = None
        try:
            hello_raw = await asyncio.wait_for(transport.recv(), 15.0)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            await transport.close(TRANSPORT_BAD_HELLO, "no hello")
            return
        except Exception:  # pragma: no cover
            await transport.close(TRANSPORT_BAD_HELLO, "bad hello")
            return

        if hello_raw is None:
            await transport.close(TRANSPORT_BAD_HELLO, "closed")
            return
        try:
            hello = protocol.parse_hello(hello_raw)
        except protocol.ProtocolError as exc:
            LOG.warning("bad hello from %s: %s", transport.remote, exc)
            await transport.send({"t": "error", "code": "bad_hello", "detail": str(exc)})
            await transport.close(TRANSPORT_BAD_HELLO, "bad hello")
            return

        node_id = hello["node_id"] or token_node_id
        if not node_id:
            await transport.send({"t": "error", "code": "missing_node_id"})
            await transport.close(TRANSPORT_BAD_HELLO, "missing node id")
            return
        if token_node_id and node_id != token_node_id:
            LOG.warning("node %s tried to use the token of %s", node_id, token_node_id)
            await transport.send({"t": "error", "code": "wrong_token", "node": token_node_id})
            await transport.close(TRANSPORT_BAD_HELLO, "token/node mismatch")
            return

        async with self.lock:
            old = self._sessions.get(node_id)
            if old is not None and old is not transport:
                LOG.info("node %s reconnected, replacing previous session", node_id)
                await self._close_session(old, TRANSPORT_REPLACED, "replaced by a new connection")

            now = time.time()
            session = NodeSession(
                node_id=node_id,
                transport=transport,
                connected_at=now,
                last_rx=now,
                first_seen=now,
                board=hello.get("board", ""),
                fw=hello.get("fw", ""),
                ip=hello.get("ip") or transport.remote,
                rssi=int(hello["rssi"]) if isinstance(hello.get("rssi"), (int, float)) else None,
                uptime=float(hello["uptime"]) if isinstance(hello.get("uptime"), (int, float)) else None,
            )
            self._sessions[node_id] = session
            self.store.upsert_node(
                node_id, board=session.board, fw=session.fw, ip=session.ip, rssi=session.rssi, now=now
            )

        LOG.info("node %s connected from %s (%d devices)", node_id, session.ip, len(hello["devices"]))
        try:
            await transport.send(protocol.hello(self.config.devices, node_id=node_id, server_time=time.time()))
            self._register_node_devices(session, hello["devices"])
            await self._reconcile_node(session)
            self._broadcast_nodes()
            for uid in session.devices:
                self._broadcast_device(uid)
            self.store.add_event("node_online", name=node_id, data={"ip": session.ip})

            session.watchdog = asyncio.create_task(self._watchdog(session), name="watch-%s" % node_id)
            await self._read_loop(session)
        finally:
            await self._end_session(session)

    async def _read_loop(self, session: NodeSession) -> None:
        while True:
            try:
                raw = await session.transport.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.info("node %s receive error: %s", session.node_id, exc)
                return
            if raw is None:
                return
            session.last_rx = time.time()
            try:
                self._handle_node_message(session, raw)
            except protocol.ProtocolError as exc:
                LOG.warning("node %s sent a bad message: %s", session.node_id, exc)
            except Exception:  # noqa: BLE001 - never let one message kill the session
                LOG.exception("error handling message from node %s", session.node_id)

    def _handle_node_message(self, session: NodeSession, raw: Dict[str, Any]) -> None:
        kind = raw.get("t")
        if kind == protocol.STATE:
            uid, values = protocol.parse_state(raw)
            self._apply_reported(session, uid, values)
        elif kind == protocol.ACK:
            self._handle_ack(session, protocol.parse_ack(raw))
        elif kind == protocol.PONG:
            seq = raw.get("n")
            if isinstance(seq, int):
                sent = session.pending_pings.pop(seq, None)
                if sent is not None:
                    session.rtt_ms = round((time.time() - sent) * 1000.0, 1)
                    self.broadcast({"t": "node", "node": self._node_public(session)})
        elif kind == protocol.PING:
            seq = raw.get("n")
            asyncio.create_task(
                self._safe_send(session, protocol.pong(int(seq) if isinstance(seq, int) else 0))
            )
        elif kind == protocol.EVENT:
            event = protocol.parse_event(raw)
            self._handle_event(session, event)
        elif kind == protocol.LOG:
            LOG.info("[node %s] %s", session.node_id, raw.get("msg", ""))
        elif kind == protocol.NODE_HELLO:
            # A node may re-announce itself (e.g. after adding a device at runtime).
            try:
                hello = protocol.parse_hello(raw)
            except protocol.ProtocolError:
                return
            if hello["node_id"] and hello["node_id"] != session.node_id:
                raise protocol.ProtocolError("hello node id does not match the session")
            session.board = hello.get("board", session.board)
            session.fw = hello.get("fw", session.fw)
            self._register_node_devices(session, hello["devices"])
            asyncio.create_task(self._reconcile_node(session))
        else:
            raise protocol.ProtocolError("unknown message type %r" % (kind,))

    async def _safe_send(self, session: NodeSession, message: Dict[str, Any]) -> None:
        try:
            await session.transport.send(message)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # device registration / reconciliation
    # ------------------------------------------------------------------ #
    def _claim_device(self, session: NodeSession, uid: str, device: Dict[str, Any]) -> bool:
        """Bind an unassigned device to this node; refuse to steal one.

        A device id belongs to exactly one node.  Without this check any board
        with a valid token could take over another board's device simply by
        reporting that id, which would also let it rewrite its desired state.
        """
        owner = device.get("node_id")
        if owner == session.node_id:
            return True
        if owner:
            LOG.warning("node %s tried to claim %s, which belongs to %s",
                        session.node_id, uid, owner)
            asyncio.create_task(self._safe_send(
                session,
                {"t": "error", "dev": uid, "code": "device_owned_by_another_node",
                 "detail": owner},
            ))
            return False
        self.store.update_device(uid, node_id=session.node_id)
        device["node_id"] = session.node_id
        return True

    def _register_node_devices(self, session: NodeSession, entries: Iterable[Dict[str, Any]]) -> None:
        known: Set[str] = set()
        created = False
        entries = list(entries)
        if len(entries) > MAX_DEVICES_PER_NODE:
            LOG.warning("node %s announced %d devices, only the first %d are used",
                        session.node_id, len(entries), MAX_DEVICES_PER_NODE)
            entries = entries[:MAX_DEVICES_PER_NODE]
        for entry in entries:
            uid = entry["uid"]
            dtype = self.registry.get(entry["type"])
            if dtype is None:
                LOG.warning("node %s reports unknown device type %r for %s", session.node_id, entry["type"], uid)
                asyncio.create_task(
                    self._safe_send(
                        session,
                        {"t": "error", "dev": uid, "code": "unknown_type", "detail": entry["type"]},
                    )
                )
                continue
            device = self.store.get_device(uid)
            if device is not None and not self._claim_device(session, uid, device):
                continue
            known.add(uid)
            reported, _ = protocol.normalize_props(dtype, entry.get("state") or {},
                                                  only_settable=False, ignore_unknown=True)
            if device is None:
                if not self._valid_uid(uid):
                    LOG.warning("node %s reports an invalid device id %r", session.node_id, uid)
                    continue
                try:
                    # Lenient: a node reporting one extra config field must not
                    # lose all of its hardware settings.
                    clean_config = dtype.validate_config(entry.get("config") or {}, strict=False)
                except ManifestError as exc:
                    LOG.warning("device %s has an invalid config: %s", uid, exc)
                    clean_config = dtype.default_config()
                # Adopt the hardware's current state instead of pushing defaults
                # onto a device the hub has never seen before.  Read-only
                # properties stay out of ``desired``: they are not something the
                # user can ask for, and storing them would be misleading.
                initial_desired = dtype.default_props()
                for key, value in reported.items():
                    spec = dtype.properties.get(key) or {}
                    if spec.get("settable", True):
                        initial_desired[key] = value
                record = {
                    "uid": uid,
                    "type": dtype.id,
                    "name": entry.get("name") or uid,
                    "room": "",
                    "node_id": session.node_id,
                    "config": clean_config,
                    "desired": initial_desired,
                    "reported": reported,
                    "sort_index": self.store.next_sort_index(),
                }
                self.store.insert_device(record)
                self.store.add_event("device_joined", uid=uid, name=record["name"], data={"node": session.node_id})
                self._broadcast_event(self.store.list_events(limit=1)[0])
                LOG.info("device %s (%s) joined from node %s", uid, dtype.id, session.node_id)
                created = True
            else:
                if entry.get("name") and device["name"] == device["uid"]:
                    self.store.update_device(uid, name=entry["name"])
                self._apply_reported(session, uid, reported, announce=False)
            self._broadcast_device(uid)
        session.devices = known
        if created:
            self._broadcast_nodes()

    async def _reconcile_node(self, session: NodeSession) -> None:
        """Push desired state that the node is not reporting yet."""
        for uid in list(session.devices):
            device = self.store.get_device(uid)
            if device is None:
                continue
            dtype = self.registry.get(device["type"])
            if dtype is None:
                continue
            pending = protocol.diff_desired(dtype, device.get("desired") or {}, device.get("reported") or {})
            if pending:
                LOG.info("reconciling %s on %s: %s", uid, session.node_id, pending)
                await self._dispatch(session, uid, pending)

    def _apply_reported(self, session: NodeSession, uid: str, values: Dict[str, Any], *,
                        announce: bool = True) -> None:
        device = self.store.get_device(uid)
        if device is None:
            LOG.debug("state for unknown device %r ignored", uid)
            return
        if not self._claim_device(session, uid, device):
            return
        dialect = self.registry.get(device["type"])
        if dialect is not None:
            values, _ = protocol.normalize_props(dialect, values, only_settable=False, ignore_unknown=True)
        if len(values) > MAX_PROPERTY_KEYS:
            raise protocol.ProtocolError("too many properties in a state message")
        reported = dict(device.get("reported") or {})
        changed = {k: v for k, v in values.items() if not protocol.values_equal(reported.get(k), v)}
        if not changed:
            return
        reported.update(changed)
        self.store.update_device(uid, reported=reported)
        self._dirty.add(uid)
        if self._should_record_history(device, changed):
            self.store.add_history(uid, {"v": changed})
        if announce:
            self._broadcast_device(uid)

    def _should_record_history(self, device: Dict[str, Any], changed: Dict[str, Any]) -> bool:
        # Slider drags generate many updates; only sample every 5s unless the
        # device is a sensor (its readings are the interesting bit).
        dtype = self.registry.get(device["type"])
        if dtype is not None and dtype.category == "sensor":
            return True
        now = time.time()
        last = self._hist_ts.get(device["uid"], 0.0)
        if now - last >= 5.0:
            self._hist_ts[device["uid"]] = now
            return True
        return False

    def _handle_ack(self, session: NodeSession, ack: Dict[str, Any]) -> None:
        pending = self._pending.get(ack["id"])
        if pending is None:
            LOG.debug("ack for unknown command %s", ack["id"])
            return
        if pending.node_id != session.node_id:
            LOG.warning("ack for %s came from the wrong node", ack["id"])
            return
        if ack["ok"]:
            self._device_errors.pop(pending.uid, None)
            values = ack["state"] or pending.props
            self._apply_reported(session, pending.uid, values)
            if pending.future and not pending.future.done():
                pending.future.set_result(True)
        else:
            self._device_errors[pending.uid] = ack["error"] or "device rejected the command"
            if pending.future and not pending.future.done():
                pending.future.set_exception(DeviceError(self._device_errors[pending.uid]))
            self.broadcast(
                {"t": "toast", "level": "error", "text": "%s: %s" % (pending.uid, self._device_errors[pending.uid])}
            )

    def _handle_event(self, session: NodeSession, event: Dict[str, Any]) -> None:
        uid = event.get("dev") or ""
        name = ""
        if uid:
            device = self.store.get_device(uid)
            name = device["name"] if device else ""
        self.store.add_event(
            "device_event", uid=uid, name=name,
            data={"event": event["event"], "data": event["data"], "node": session.node_id},
        )
        LOG.info("event %s from %s (%s)", event["event"], uid or session.node_id, event["data"])
        events = self.store.list_events(limit=1)
        if events:
            self.broadcast({"t": "event", "event": self._event_public(events[0])})

    @staticmethod
    def _event_public(event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": event.get("id"),
            "ts": event.get("ts"),
            "kind": event.get("kind"),
            "uid": event.get("uid"),
            "name": event.get("name"),
            "data": event.get("data") or {},
        }

    def _broadcast_event(self, event: Dict[str, Any]) -> None:
        self.broadcast({"t": "event", "event": self._event_public(event)})

    async def _watchdog(self, session: NodeSession) -> None:
        cfg = self.config.devices
        heartbeat = max(0.25, float(cfg["heartbeat_s"]))
        offline_after = max(heartbeat * 3, float(cfg["offline_after_s"]))
        # Tick often enough that the silence timeout is detected promptly, but
        # never more than once a second (this is a battery powered microcontroller
        # on the other end).
        tick = max(0.1, min(1.0, offline_after / 4.0, heartbeat))
        try:
            while True:
                await asyncio.sleep(tick)
                if self._closing or not session.online:
                    return
                now = time.time()
                if now - session.last_rx > offline_after:
                    LOG.warning("node %s silent for %.0fs, closing", session.node_id, now - session.last_rx)
                    await self._close_session(session, 4008, "heartbeat timeout")
                    return
                if now - session.last_rx >= heartbeat:
                    session.ping_seq += 1
                    session.pending_pings[session.ping_seq] = now
                    if len(session.pending_pings) > 8:
                        session.pending_pings.pop(min(session.pending_pings), None)
                    await self._safe_send(session, protocol.ping(session.ping_seq, now))
        except asyncio.CancelledError:
            raise

    async def _close_session(self, session: NodeSession, code: int, reason: str) -> None:
        try:
            await session.transport.close(code, reason)
        except Exception:  # noqa: BLE001
            pass
        await self._end_session(session)

    async def _end_session(self, session: NodeSession) -> None:
        if session.watchdog and not session.watchdog.done():
            session.watchdog.cancel()
        if session.transport.connected:
            try:
                await session.transport.close(1000, "bye")
            except Exception:  # noqa: BLE001
                pass
        if self._sessions.get(session.node_id) is session:
            self._sessions.pop(session.node_id, None)
            # Fail all in-flight commands for this node: they are retried on reconnect.
            for msg_id, pending in list(self._pending.items()):
                if pending.node_id == session.node_id:
                    if pending.future and not pending.future.done():
                        pending.future.set_exception(OfflineNode("node disconnected"))
            self._flush_dirty()
            LOG.info("node %s disconnected", session.node_id)
            self._broadcast_nodes()
            for uid in session.devices:
                self._broadcast_device(uid)
            self.store.add_event("node_offline", name=session.node_id)

    def node_sessions(self) -> List[Dict[str, Any]]:
        return [self._node_public(s) for s in self._sessions.values()]

    # ------------------------------------------------------------------ #
    # node management
    # ------------------------------------------------------------------ #
    def list_nodes(self) -> List[Dict[str, Any]]:
        return self.snapshot()["nodes"]

    def delete_node(self, node_id: str) -> None:
        for uid in [d["uid"] for d in self.store.devices_for_node(node_id)]:
            self.store.update_device(uid, node_id=None)
        self.store.delete_node(node_id)
        self._broadcast_nodes()

    # ------------------------------------------------------------------ #
    # background tasks
    # ------------------------------------------------------------------ #
    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(2.0)
                self._flush_dirty()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            LOG.exception("flush loop crashed")

    def _flush_dirty(self) -> None:
        if not self._dirty:
            return
        for uid in list(self._dirty):
            device = self.store.get_device(uid)
            if device is None:
                self._dirty.discard(uid)
                continue
            self.store.update_device(uid, reported=device.get("reported") or {})
            self._dirty.discard(uid)

    async def _prune_loop(self) -> None:
        try:
            await asyncio.sleep(60)
            while True:
                days = max(1, int(self.config.devices.get("history_days", 30)))
                cutoff = time.time() - days * 86400
                removed = self.store.prune_history(cutoff)
                if removed:
                    LOG.info("pruned %d history rows", removed)
                await asyncio.sleep(6 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            LOG.exception("prune loop crashed")

    # ------------------------------------------------------------------ #
    # misc helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _valid_uid(uid: str) -> bool:
        if not uid or len(uid) > 64:
            return False
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
        return all(ch in allowed for ch in uid) and not uid.startswith(("-", "."))

    def _new_uid(self, type_id: str) -> str:
        base = type_id.replace("_", "-")
        for _ in range(10):
            candidate = "%s-%s" % (base, uuid.uuid4().hex[:6])
            if self.store.get_device(candidate) is None:
                return candidate
        return "%s-%s" % (base, uuid.uuid4().hex[:10])

    def reload_types(self) -> Dict[str, str]:
        errors = self.registry.reload()
        self.broadcast(
            {"t": "types", "types": self.registry.public(), "type_errors": errors}
        )
        return errors

    def update_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "heartbeat_s", "report_s", "offline_after_s", "ack_timeout_s",
            "ack_retries", "history_days", "title", "theme",
        }
        unknown = set(values) - allowed
        if unknown:
            raise DeviceError("unknown setting(s): %s" % ", ".join(sorted(unknown)))
        dev = self.config.devices
        ui = self.config.ui
        for key, value in values.items():
            if key in ("title", "theme"):
                ui[key] = str(value)
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise DeviceError("%s must be a number" % key)
            if key == "ack_retries":
                dev[key] = max(0, min(10, int(number)))
            elif key == "ack_timeout_s":
                dev[key] = max(0.5, min(60.0, number))
            elif key == "heartbeat_s":
                dev[key] = max(1.0, min(3600.0, number))
            elif key == "report_s":
                dev[key] = max(1.0, min(86400.0, number))
            elif key == "offline_after_s":
                dev[key] = max(3.0, min(86400.0, number))
            elif key == "history_days":
                dev[key] = max(1, min(3650, int(number)))
        if dev["offline_after_s"] < dev["heartbeat_s"] * 2:
            if "offline_after_s" in values:
                raise DeviceError("offline timeout must be at least twice the heartbeat interval")
            # A user raising the heartbeat should not have to think about the
            # watchdog as well: keep it comfortably above the new interval.
            dev["offline_after_s"] = max(dev["offline_after_s"], dev["heartbeat_s"] * 3)
        self.config.save()
        config_message = protocol.config_message(dev)
        for session in list(self._sessions.values()):
            asyncio.create_task(self._safe_send(session, config_message))
        self.broadcast({"t": "settings", "settings": self._public_settings()})
        return self._public_settings()

    def history(self, uid: str, *, hours: float = 24.0) -> List[Dict[str, Any]]:
        self.get_device(uid)
        since = time.time() - max(0.1, min(hours, 24 * 90)) * 3600
        return self.store.query_history(uid, since=since)
