"""SQLite persistence.

The schema is deliberately boring: one row per device with JSON columns for the
configuration and for the desired/reported property maps.  Writes are batched
by the caller (see :mod:`smarthome.hub`) so an SD card is not hammered.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    board       TEXT NOT NULL DEFAULT '',
    fw          TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    rssi        INTEGER,
    token_hash  TEXT NOT NULL DEFAULT '',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    uid         TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    room        TEXT NOT NULL DEFAULT '',
    node_id     TEXT REFERENCES nodes(node_id) ON DELETE SET NULL,
    config      TEXT NOT NULL DEFAULT '{}',
    desired     TEXT NOT NULL DEFAULT '{}',
    reported    TEXT NOT NULL DEFAULT '{}',
    sort_index  INTEGER NOT NULL DEFAULT 0,
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS devices_node ON devices(node_id);
CREATE INDEX IF NOT EXISTS devices_type ON devices(type);

CREATE TABLE IF NOT EXISTS history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    uid     TEXT NOT NULL,
    ts      REAL NOT NULL,
    values_ TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS history_uid_ts ON history(uid, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    uid     TEXT NOT NULL DEFAULT '',
    name    TEXT NOT NULL DEFAULT '',
    data    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


def _loads(text: Optional[str], fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        value = json.loads(text)
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class Store:
    """Thread-safe wrapper around the SQLite database."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # low level helpers
    # ------------------------------------------------------------------ #
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ #
    # nodes
    # ------------------------------------------------------------------ #
    def upsert_node(self, node_id: str, *, board: str = "", fw: str = "", ip: str = "",
                    rssi: Optional[int] = None, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        self._execute(
            """
            INSERT INTO nodes(node_id, board, fw, ip, rssi, first_seen, last_seen)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                board=excluded.board,
                fw=excluded.fw,
                ip=excluded.ip,
                rssi=excluded.rssi,
                last_seen=excluded.last_seen
            """,
            (node_id, board, fw, ip, rssi, ts, ts),
        )

    def touch_node(self, node_id: str, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        self._execute("UPDATE nodes SET last_seen=? WHERE node_id=?", (ts, node_id))

    def list_nodes(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM nodes ORDER BY node_id")
        return [dict(row) for row in rows]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM nodes WHERE node_id=?", (node_id,))
        return dict(rows[0]) if rows else None

    def delete_node(self, node_id: str) -> None:
        self._execute("DELETE FROM nodes WHERE node_id=?", (node_id,))

    def node_token_hash(self, node_id: str) -> str:
        rows = self._query("SELECT token_hash FROM nodes WHERE node_id=?", (node_id,))
        return rows[0]["token_hash"] if rows else ""

    def set_node_token_hash(self, node_id: str, token_hash: str) -> None:
        now = time.time()
        self._execute(
            """
            INSERT INTO nodes(node_id, token_hash, first_seen, last_seen)
            VALUES(?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET token_hash=excluded.token_hash
            """,
            (node_id, token_hash, now, now),
        )

    # ------------------------------------------------------------------ #
    # devices
    # ------------------------------------------------------------------ #
    def _row_to_device(self, row: sqlite3.Row) -> Dict[str, Any]:
        device = dict(row)
        device["config"] = _loads(device.get("config"), {})
        device["desired"] = _loads(device.get("desired"), {})
        device["reported"] = _loads(device.get("reported"), {})
        return device

    def list_devices(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM devices ORDER BY sort_index, name COLLATE NOCASE")
        return [self._row_to_device(row) for row in rows]

    def get_device(self, uid: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM devices WHERE uid=?", (uid,))
        return self._row_to_device(rows[0]) if rows else None

    def devices_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM devices WHERE node_id=? ORDER BY sort_index, name", (node_id,))
        return [self._row_to_device(row) for row in rows]

    def next_sort_index(self) -> int:
        rows = self._query("SELECT COALESCE(MAX(sort_index), 0) + 1 AS n FROM devices")
        return int(rows[0]["n"]) if rows else 1

    def insert_device(self, device: Dict[str, Any]) -> None:
        now = time.time()
        self._execute(
            """
            INSERT INTO devices(uid, type, name, room, node_id, config, desired, reported,
                                sort_index, created, updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device["uid"],
                device["type"],
                device["name"],
                device.get("room", ""),
                device.get("node_id"),
                json.dumps(device.get("config") or {}, separators=(",", ":")),
                json.dumps(device.get("desired") or {}, separators=(",", ":")),
                json.dumps(device.get("reported") or {}, separators=(",", ":")),
                int(device.get("sort_index") or self.next_sort_index()),
                now,
                now,
            ),
        )

    def update_device(self, uid: str, **fields: Any) -> None:
        allowed = {"type", "name", "room", "node_id", "config", "desired", "reported", "sort_index"}
        parts: List[str] = []
        params: List[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("cannot update field %r" % (key,))
            if key in ("config", "desired", "reported"):
                value = json.dumps(value or {}, separators=(",", ":"))
            parts.append("%s=?" % key)
            params.append(value)
        if not parts:
            return
        parts.append("updated=?")
        params.append(time.time())
        params.append(uid)
        self._execute("UPDATE devices SET %s WHERE uid=?" % ", ".join(parts), params)

    def delete_device(self, uid: str) -> None:
        self._execute("DELETE FROM devices WHERE uid=?", (uid,))
        self._execute("DELETE FROM history WHERE uid=?", (uid,))
        self._execute("DELETE FROM events WHERE uid=?", (uid,))

    # ------------------------------------------------------------------ #
    # history
    # ------------------------------------------------------------------ #
    def add_history(self, uid: str, values: Dict[str, Any], now: Optional[float] = None) -> None:
        if not values:
            return
        ts = time.time() if now is None else now
        self._execute(
            "INSERT INTO history(uid, ts, values_) VALUES(?,?,?)",
            (uid, ts, json.dumps(values, separators=(",", ":"))),
        )

    def query_history(self, uid: str, *, since: float, until: Optional[float] = None,
                      limit: int = 2000) -> List[Dict[str, Any]]:
        until = time.time() if until is None else until
        rows = self._query(
            "SELECT ts, values_ FROM history WHERE uid=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
            (uid, since, until, limit),
        )
        out = [{"ts": row["ts"], "values": _loads(row["values_"], {})} for row in rows]
        out.reverse()
        return out

    def prune_history(self, older_than: float) -> int:
        cursor = self._execute("DELETE FROM history WHERE ts < ?", (older_than,))
        cursor2 = self._execute("DELETE FROM events WHERE ts < ?", (older_than,))
        return (cursor.rowcount or 0) + (cursor2.rowcount or 0)

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def add_event(self, kind: str, *, uid: str = "", name: str = "", data: Optional[Dict[str, Any]] = None,
                  now: Optional[float] = None) -> int:
        ts = time.time() if now is None else now
        cursor = self._execute(
            "INSERT INTO events(ts, kind, uid, name, data) VALUES(?,?,?,?,?)",
            (ts, kind, uid, name, json.dumps(data or {}, separators=(",", ":"))),
        )
        return int(cursor.lastrowid or 0)

    def list_events(self, *, limit: int = 100, uid: Optional[str] = None,
                    since: Optional[float] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events"
        where: List[str] = []
        params: List[Any] = []
        if uid:
            where.append("uid=?")
            params.append(uid)
        if since is not None:
            where.append("ts>=?")
            params.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._query(sql, params)
        events = []
        for row in rows:
            item = dict(row)
            item["data"] = _loads(item.get("data"), {})
            events.append(item)
        return events

    def last_history_event(self, uid: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM events WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,))
        if not rows:
            return None
        item = dict(rows[0])
        item["data"] = _loads(item.get("data"), {})
        return item


def open_store(path: Path) -> Store:
    return Store(path)
