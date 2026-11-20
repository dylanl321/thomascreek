"""SQLite store for people, devices, the activity log, inbox and requests.

The household is small (a handful of people, a few dozen devices), so a single
SQLite file on the NAS dataset with WAL mode is plenty and is easy to snapshot.
"""

from __future__ import annotations

import contextvars
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
    # AI connection keys for the hub's MCP server (hub/aikeys.py).
    """
    CREATE TABLE IF NOT EXISTS ai_keys (
        id TEXT PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        person_id TEXT NOT NULL,
        device_id TEXT NOT NULL,
        created_by TEXT,
        created_at REAL NOT NULL,
        revoked_at REAL,
        last_used_at REAL
    )""",
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
    # Films, shows and YouTube a grown-up sent to someone's iPad (hub/sends.py).
    """
    CREATE TABLE IF NOT EXISTS sends (
        id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT NOT NULL,
        title TEXT NOT NULL,
        poster_url TEXT,
        scope TEXT NOT NULL DEFAULT 'one',
        count INTEGER,
        season INTEGER,
        quality TEXT NOT NULL DEFAULT 'high',
        created_by TEXT,
        created_at REAL NOT NULL,
        last_error TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS send_devices (
        device_id TEXT PRIMARY KEY,
        person_id TEXT,
        holdings TEXT NOT NULL DEFAULT '[]',
        free_bytes INTEGER,
        total_bytes INTEGER,
        reported_at REAL NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS send_watched (
        person_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        watched_at REAL NOT NULL,
        PRIMARY KEY (person_id, item_id)
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
    # When each hub_kv row last changed, kept apart from the value. In hub_kv `updated_at` sits after `value`, so
    # SQLite walked the whole overflow chain of a several-MB value to read it: 0.3 s per stamp of the folded
    # catalog, read four to six times per episode play (Nov 20 2026). `kv_set_text` writes both in one transaction.
    """
    CREATE TABLE IF NOT EXISTS hub_kv_stamps (
        key TEXT PRIMARY KEY,
        updated_at REAL NOT NULL
    )""",
    # One row per series, film or episode page the hub read from a provider (hub/tvcatalog.py `_details`). It was
    # one JSON file rewritten whole for every page, which held the interpreter for seconds per episode play.
    """
    CREATE TABLE IF NOT EXISTS tv_details (
        bucket TEXT NOT NULL,
        id TEXT NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (bucket, id)
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

# The house brain's run log (hub/brain): every background AI run and every assistant turn, with its cost.
SCHEMA += [
    """
    CREATE TABLE IF NOT EXISTS ai_runs (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        job TEXT NOT NULL,
        trigger TEXT,
        input_summary TEXT,
        input_hash TEXT,
        model TEXT,
        tier TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cached INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        decision TEXT,
        action TEXT,
        activity_id INTEGER,
        card_id TEXT,
        outcome TEXT,
        feedback TEXT,
        feedback_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS ai_runs_ts ON ai_runs(ts)",
    "CREATE INDEX IF NOT EXISTS ai_runs_job ON ai_runs(job, ts)",
    "CREATE INDEX IF NOT EXISTS ai_runs_card ON ai_runs(card_id)",
]

# Holiday and feast plans (hub/feasts.py): Mealie has day-by-day meal plans but no named event with courses.
SCHEMA += [
    """
    CREATE TABLE IF NOT EXISTS feast_plans (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'feast',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        guests INTEGER NOT NULL DEFAULT 8,
        notes TEXT,
        courses TEXT NOT NULL DEFAULT '[]',
        created_by TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
]

# Watch history, one row per person, TV or profile ("bucket") and title (#210). It was one hub_kv document,
# "tv.watch", rewritten on every heartbeat; DB() carries that document over once (_move_watch_history).
SCHEMA += [
    """
    CREATE TABLE IF NOT EXISTS watch_history (
        bucket TEXT NOT NULL,
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        data TEXT NOT NULL,
        last REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (bucket, kind, key)
    )""",
]
WATCH_KINDS = ("channels", "series", "movies")
WATCH_UPSERT = "INSERT OR REPLACE INTO watch_history (bucket, kind, key, data, last) VALUES (?,?,?,?,?)"

ADDED_COLUMNS = [
    ("devices", "kind", "TEXT NOT NULL DEFAULT 'phone'"),   # phone | test (a simulator joined with a test key)
    ("devices", "test_key_id", "TEXT"),
    ("yt_sources", "latest", "INTEGER"),   # a channel sent to the kids' iPads: how many newest videos to keep
    ("yt_sources", "max_height", "INTEGER"),  # quality picked in the wizard (480/720/1080); NULL = the hub's max_height
]

JSON_COLUMNS = {"decision", "holdings", "source_ids", "areas", "shared_areas", "grants", "denies", "prefs", "activity", "actions", "payload", "params"}


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


# A caller that wants to know how long its writes waited for the shared connection sets a dict here (the play
# route does, for its Server-Timing); `execute` adds each wait to "db_wait", in milliseconds.
LOCK_WAITS: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar("db_lock_waits", default=None)


class DB:
    def __init__(self, path: Path | str):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Reads use a connection of their own per thread (WAL lets them run while another connection writes), so
        # a request's small read no longer waits for a worker thread's big write or read on the shared
        # connection (Nov 19 2026: the live hub stalled 3 to 7 s on `kv_get` and `people` behind them).
        self._readers = threading.local()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # In WAL mode NORMAL is SQLite's own advice: the database is never corrupted, and only the last commits
            # before a power cut can be lost. FULL synced the disk on every commit, and a play makes several
            # (lease, title, log) while the NAS is busy (Nov 20 2026).
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for stmt in SCHEMA:
                self._conn.execute(stmt)
            for table, col, ddl in ADDED_COLUMNS:
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            self._move_watch_history()
            # Stamps for rows written before the stamps table existed (read once; later writes keep it).
            self._conn.execute("INSERT OR IGNORE INTO hub_kv_stamps (key, updated_at) SELECT key, updated_at "
                               "FROM hub_kv WHERE key NOT IN (SELECT key FROM hub_kv_stamps)")

    def _move_watch_history(self) -> None:
        """Carry the old "tv.watch" document into watch_history once, then drop it. Runs inside __init__'s lock."""
        row = self._conn.execute("SELECT value FROM hub_kv WHERE key='tv.watch'").fetchone()
        if row is None:
            return
        try:
            doc = json.loads(row["value"])
        except json.JSONDecodeError:
            doc = {}
        self._conn.execute("BEGIN")
        try:
            for bucket, kinds in (doc.items() if isinstance(doc, dict) else []):
                if not isinstance(kinds, dict):
                    continue
                for kind in WATCH_KINDS:
                    for key, item in (kinds.get(kind) or {}).items():
                        if isinstance(item, dict):
                            self._conn.execute(
                                WATCH_UPSERT,
                                [str(bucket), kind, str(key), json.dumps(item), float(item.get("last") or 0)])
            self._conn.execute("DELETE FROM hub_kv WHERE key='tv.watch'")
            self._conn.execute("DELETE FROM hub_kv_stamps WHERE key='tv.watch'")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # -- watch history (hub/upnext.py) -----------------------------------
    def watch_bucket(self, bucket: str) -> dict[str, dict[str, Any]]:
        """One bucket in the shape the history readers use: {"channels": {key: row}, "series": …, "movies": …}."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.all("SELECT kind, key, data FROM watch_history WHERE bucket=?", [bucket]):
            out.setdefault(r["kind"], {})[r["key"]] = json.loads(r["data"])
        return out

    def watch_buckets(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in self.all("SELECT bucket, kind, key, data FROM watch_history"):
            out.setdefault(r["bucket"], {}).setdefault(r["kind"], {})[r["key"]] = json.loads(r["data"])
        return out

    def details_rows(self) -> list[tuple[str, str, str]]:
        """Every saved page: (bucket, id, JSON text)."""
        return [(r["bucket"], r["id"], r["data"]) for r in self.all("SELECT bucket, id, data FROM tv_details")]

    def details_put(self, rows: list[tuple[str, str, str]]) -> None:
        """Save pages (bucket, id, JSON text) in one transaction."""
        if not rows:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany("INSERT INTO tv_details (bucket, id, data) VALUES (?,?,?) "
                                       "ON CONFLICT(bucket, id) DO UPDATE SET data=excluded.data", rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def watch_save(self, bucket: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        """Write only the rows that changed, and drop the ones `after` no longer keeps (trimmed)."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for kind in WATCH_KINDS:
                    old, new = before.get(kind) or {}, after.get(kind) or {}
                    for key, item in new.items():
                        if old.get(key) != item:
                            self._conn.execute(
                                WATCH_UPSERT,
                                [bucket, kind, str(key), json.dumps(item), float(item.get("last") or 0)])
                    for key in old.keys() - new.keys():
                        self._conn.execute("DELETE FROM watch_history WHERE bucket=? AND kind=? AND key=?",
                                           [bucket, kind, str(key)])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- low level -------------------------------------------------------
    def execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        started = time.perf_counter()
        with self._lock:
            waited = time.perf_counter() - started
            found = LOCK_WAITS.get()
            if found is not None:
                found["db_wait"] = round(found.get("db_wait", 0.0) + waited * 1000, 1)
            return self._conn.execute(sql, tuple(args))

    def _reader(self) -> sqlite3.Connection | None:
        """This thread's read connection, or None for an in-memory database (it has only the one connection) and
        while this thread is inside a transaction on the shared connection (it must read its own writes).
        Another thread's transaction (a batch of saved pages, say) no longer sends this read to wait behind it:
        transactions run only under `_lock`, so one is this thread's only when this thread holds the lock."""
        owned = self._lock._is_owned()  # type: ignore[attr-defined]
        if self._path == ":memory:" or (self._conn.in_transaction and owned):
            return None
        conn = getattr(self._readers, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
            except sqlite3.Error:
                return None
            self._readers.conn = conn
        return conn

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        reader = self._reader()
        if reader is not None:
            return _row(reader.execute(sql, tuple(args)).fetchone())
        with self._lock:
            return _row(self._conn.execute(sql, tuple(args)).fetchone())

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        reader = self._reader()
        if reader is not None:
            return [_row(r) for r in reader.execute(sql, tuple(args)).fetchall()]  # type: ignore[misc]
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

    def kv_stamp(self, key: str) -> float | None:
        """When a setting last changed, without reading its value (some hold large maps): from hub_kv_stamps."""
        row = self.one("SELECT updated_at FROM hub_kv_stamps WHERE key=?", [key])
        return float(row["updated_at"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.kv_set_text(key, json.dumps(value))

    def kv_set_text(self, key: str, text: str) -> None:
        """`kv_set` with the value already written as JSON (a big one is written gently, see `gentlejson.dumps`)."""
        now = time.time()
        with self._lock:
            own = not self._conn.in_transaction
            if own:
                self._conn.execute("BEGIN")
            try:
                self.execute("INSERT INTO hub_kv (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                             "SET value=excluded.value, updated_at=excluded.updated_at", [key, text, now])
                self.execute("INSERT INTO hub_kv_stamps (key, updated_at) VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                             "SET updated_at=excluded.updated_at", [key, now])
                if own:
                    self._conn.execute("COMMIT")
            except Exception:
                if own:
                    self._conn.execute("ROLLBACK")
                raise

    def kv_delete(self, key: str) -> None:
        with self._lock:
            self.execute("DELETE FROM hub_kv WHERE key=?", [key])
            self.execute("DELETE FROM hub_kv_stamps WHERE key=?", [key])

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
