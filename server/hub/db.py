"""SQLite store for people, devices, the activity log, inbox and requests.

The household is small (a handful of people, a few dozen devices), so a single
SQLite file on the NAS dataset with WAL mode is plenty and is easy to snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS people (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        avatar TEXT,
        ui_style TEXT NOT NULL DEFAULT 'standard',
        areas TEXT NOT NULL DEFAULT '[]',
        shared_areas TEXT NOT NULL DEFAULT '[]',
        grants TEXT NOT NULL DEFAULT '[]',
        denies TEXT NOT NULL DEFAULT '[]',
        tv_device TEXT,
        family_board_id TEXT,
        presence_entity TEXT,
        expires_at REAL,
        created_at REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        person_id TEXT REFERENCES people(id),
        name TEXT NOT NULL,
        model TEXT,
        platform TEXT,
        app_version TEXT,
        public_key TEXT NOT NULL UNIQUE,
        secure_public_key TEXT,
        status TEXT NOT NULL,
        apns_token TEXT,
        apns_env TEXT,
        prefs TEXT NOT NULL DEFAULT '{}',
        activity TEXT NOT NULL DEFAULT '{}',
        wg_public_key TEXT,
        wg_address TEXT,
        enrolled_ip TEXT,
        created_at REAL NOT NULL,
        last_seen_at REAL
    )""",
    """
    CREATE TABLE IF NOT EXISTS nonces (
        nonce TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        expires_at REAL NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    )""",
    """
    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        person_id TEXT,
        device_id TEXT,
        action TEXT NOT NULL,
        target TEXT,
        params TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        source TEXT NOT NULL DEFAULT 'app'
    )""",
    "CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts)",
    """
    CREATE TABLE IF NOT EXISTS inbox (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        person_id TEXT NOT NULL,
        level TEXT NOT NULL,
        cat TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT,
        actions TEXT NOT NULL DEFAULT '[]',
        ref TEXT,
        group_key TEXT,
        acked_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS inbox_person ON inbox(person_id, acked_at)",
    """
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        kind TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        title TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        poster_url TEXT,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at REAL,
        external_id TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS network_rules (
        rule_id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL,
        mac TEXT NOT NULL,
        device_name TEXT,
        gid TEXT,
        created_at REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS timers (
        id TEXT PRIMARY KEY,
        due_at REAL NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}'
    )""",
    """
    CREATE TABLE IF NOT EXISTS yt_sources (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        url TEXT NOT NULL,
        yt_id TEXT,
        title TEXT NOT NULL,
        channel TEXT,
        channel_id TEXT,
        thumb_url TEXT,
        added_by TEXT,
        created_at REAL NOT NULL,
        follow INTEGER NOT NULL DEFAULT 0,
        last_checked_at REAL,
        last_error TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS yt_videos (
        id TEXT PRIMARY KEY,
        source_ids TEXT NOT NULL DEFAULT '[]',
        title TEXT NOT NULL,
        channel TEXT,
        channel_id TEXT,
        duration_s REAL,
        uploaded TEXT,
        thumb_url TEXT,
        status TEXT NOT NULL,
        error TEXT,
        file TEXT,
        bytes INTEGER,
        height INTEGER,
        added_at REAL NOT NULL,
        ready_at REAL,
        watched_at REAL,
        position_s REAL
    )""",
    """
    CREATE TABLE IF NOT EXISTS yt_blocked (
        channel_id TEXT PRIMARY KEY,
        name TEXT,
        blocked_by TEXT,
        blocked_at REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS alert_prefs (
        person_id TEXT PRIMARY KEY,
        prefs TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS alert_log (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT,
        ref TEXT,
        image TEXT,
        people TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS alert_log_ts ON alert_log(ts)",
    """
    CREATE TABLE IF NOT EXISTS test_keys (
        id TEXT PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        person_id TEXT NOT NULL,
        created_by TEXT,
        created_at REAL NOT NULL,
        expires_at REAL,
        revoked_at REAL,
        last_used_at REAL
    )""",
    """
    CREATE TABLE IF NOT EXISTS person_settings (
        person_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL,
        device_id TEXT,
        PRIMARY KEY (person_id, key)
    )""",
    """
    CREATE TABLE IF NOT EXISTS hub_kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    )""",
]

# Columns added after the first deploy: (table, column, definition). Added on start if missing.
SCHEMA += [
    """
    CREATE TABLE IF NOT EXISTS guest_passes (
        id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        created_by TEXT,
        created_at REAL NOT NULL,
        starts_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        show_wifi INTEGER NOT NULL DEFAULT 1,
        door_name TEXT,
        lock_slot INTEGER,
        note TEXT,
        views INTEGER NOT NULL DEFAULT 0,
        last_viewed_at REAL,
        revoked_at REAL
    )""",
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        person_id TEXT,
        device_id TEXT,
        text TEXT NOT NULL,
        screen TEXT,
        app_build TEXT,
        has_shot INTEGER NOT NULL DEFAULT 0,
        done_at REAL
    )""",
]

ADDED_COLUMNS = [
    ("devices", "kind", "TEXT NOT NULL DEFAULT 'phone'"),   # phone | test (a simulator joined with a test key)
    ("devices", "test_key_id", "TEXT"),
]

JSON_COLUMNS = {"source_ids", "areas", "shared_areas", "grants", "denies", "prefs", "activity", "actions", "payload", "params"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for k in JSON_COLUMNS & out.keys():
        if isinstance(out[k], str):
            try:
                out[k] = json.loads(out[k])
            except json.JSONDecodeError:
                pass
    return out


class DB:
    def __init__(self, path: Path | str):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for stmt in SCHEMA:
                self._conn.execute(stmt)
            for table, col, ddl in ADDED_COLUMNS:
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # -- low level -------------------------------------------------------
    def execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(args))

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._conn.execute(sql, tuple(args)).fetchone())

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]  # type: ignore[misc]

    def insert(self, table: str, values: dict[str, Any]) -> None:
        cols = list(values)
        vals = [json.dumps(v) if k in JSON_COLUMNS and not isinstance(v, str) else v for k, v in values.items()]
        self.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )

    def update(self, table: str, key: str, key_value: Any, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ",".join(f"{k}=?" for k in values)
        vals = [json.dumps(v) if k in JSON_COLUMNS and not isinstance(v, str) else v for k, v in values.items()]
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*vals, key_value])

    # -- small hub-wide settings -------------------------------------------
    def kv_get(self, key: str) -> Any:
        row = self.one("SELECT value FROM hub_kv WHERE key=?", [key])
        return json.loads(row["value"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO hub_kv (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value, updated_at=excluded.updated_at", [key, json.dumps(value), time.time()])

    # -- people ----------------------------------------------------------
    def person(self, person_id: str | None) -> dict[str, Any] | None:
        if not person_id:
            return None
        return self.one("SELECT * FROM people WHERE id=?", [person_id])

    def people(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM people ORDER BY created_at")

    def add_person(self, name: str, role: str, **extra: Any) -> dict[str, Any]:
        pid = extra.pop("id", None) or new_id("per")
        self.insert("people", {"id": pid, "name": name, "role": role, "created_at": time.time(), **extra})
        return self.person(pid)  # type: ignore[return-value]

    def people_with_role(self, *roles: str) -> list[dict[str, Any]]:
        q = ",".join("?" * len(roles))
        return self.all(f"SELECT * FROM people WHERE role IN ({q})", roles)

    # -- devices ---------------------------------------------------------
    def device(self, device_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM devices WHERE id=?", [device_id])

    def devices(self, person_id: str | None = None) -> list[dict[str, Any]]:
        if person_id:
            return self.all("SELECT * FROM devices WHERE person_id=? ORDER BY created_at", [person_id])
        return self.all("SELECT * FROM devices ORDER BY created_at")

    # -- activity --------------------------------------------------------
    def log(
        self,
        action: str,
        outcome: str,
        *,
        person_id: str | None = None,
        device_id: str | None = None,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        detail: str | None = None,
        source: str = "app",
    ) -> int:
        cur = self.execute(
            "INSERT INTO activity (ts, person_id, device_id, action, target, params, outcome, detail, source)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [time.time(), person_id, device_id, action, target, json.dumps(params or {}), outcome, detail, source],
        )
        return int(cur.lastrowid or 0)

    def activity(self, before: int | None = None, limit: int = 100, person_id: str | None = None,
                 prefixes: Iterable[str] | None = None, before_ts: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM activity WHERE 1=1"
        args: list[Any] = []
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(before_ts)
        if prefixes:
            prefixes = list(prefixes)
            sql += " AND (" + " OR ".join("action LIKE ?" for _ in prefixes) + ")"
            args += [p.replace("%", "") + "%" for p in prefixes]
        if before:
            sql += " AND id < ?"
            args.append(before)
        if person_id:
            sql += " AND person_id = ?"
            args.append(person_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.all(sql, args)
