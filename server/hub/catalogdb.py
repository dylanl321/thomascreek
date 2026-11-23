"""The provider catalog as a small SQLite file for reading (hub rebuild phase 3).

The snapshot (`tvapp/catalog.json`) holds the whole provider library and guide: up to 150,000 films and series
and 100,000+ programmes. Paging, filtering and searching it in Python walked every row per call and held Python's
interpreter lock while doing it, so a search took 9 to 11 s on the live hub (Nov 19 2026) and slowed every other
call meanwhile. Here the same rows sit in `tvapp/catalog.db`, with the cleaned sort name and the lower-cased name
worked out once when the snapshot is written. SQLite does the scan without the interpreter lock.

The JSON snapshot stays the source of truth: this file is rebuilt from it (in the provider-read child after a
read, or in a worker thread when the hub finds it missing or older than the snapshot), written to a temp file and
renamed into place, so a reader sees the old file or the new one. Until it matches the snapshot, callers keep the
old in-memory path (`TvCatalog._list`), so nothing is ever blank.

Readers get rows exactly as the snapshot holds them; the kids flag, art and public shape are added by the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

from . import tvnames

log = logging.getLogger("hub.catalogdb")

VERSION = 3  # 2: programme words in their own narrow table (ptext), so a search scans words, not JSON;
# 3: `twin`, so a list shows one card per title (Nov 23 2026)
KINDS = ("movies", "series")
SORTS = {
    "name": "sort_key, ord",
    "year": "year DESC, sort_key, ord",
    "added": "added DESC, ord DESC",
}

_SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE titles (
  kind TEXT NOT NULL, ord INTEGER NOT NULL, name_cf TEXT NOT NULL, sort_key TEXT NOT NULL,
  year INTEGER NOT NULL, added REAL NOT NULL, category TEXT, category_cf TEXT, row TEXT NOT NULL,
  twin TEXT NOT NULL, lead INTEGER NOT NULL, PRIMARY KEY (kind, ord));
CREATE INDEX titles_sort ON titles (kind, sort_key, ord);
CREATE INDEX titles_category ON titles (kind, category_cf);
CREATE TABLE progs (chan_ord INTEGER NOT NULL, item_ord INTEGER NOT NULL, channel TEXT NOT NULL,
  text_cf TEXT NOT NULL, row TEXT NOT NULL, PRIMARY KEY (chan_ord, item_ord));
CREATE TABLE ptext (chan_ord INTEGER NOT NULL, item_ord INTEGER NOT NULL, text_cf TEXT NOT NULL,
  PRIMARY KEY (chan_ord, item_ord)) WITHOUT ROWID;
"""


def _year(value: object) -> int:
    text = str(value or "").strip()[:4]
    return int(text) if text.isdigit() else 0


def _added(value: object) -> float:
    try:
        return float(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# Picture and feed words a provider adds to one title's copies ("Dept. Q 4K", "Dune UHD"), as the phones' PlaylistFold.
_COPY_WORDS = re.compile(r"\b(8k|4k|uhd|fhd|hd|sd|hq|hevc|h265|h264|1080p|2160p|720p|hdr|hdr10|dv|multi|multisub)\b")


def twin_key(row: dict[str, Any], ord_: int) -> str:
    """What two copies of one title share, so a list shows it once (Nov 23 2026: "Dept. Q 4K" and "Murderbot 4K"
    twice side by side in the 4K films, two provider rows of the same title with different posters). The cleaned
    name ("EN - Dept. Q 4K (2025)" -> "dept q") and the year, so a remake stays apart. Without a year only copies in
    the same category count as one: "The Office" in two categories may be the American and the British one. A name
    that cleans to nothing is never folded."""
    name, year = tvnames.polish(row.get("name"))
    clean = " ".join(_COPY_WORDS.sub(" ", re.sub(r"[\W_]+", " ", str(name or "").casefold())).split())
    if not clean:
        return f"#{ord_}"
    year = year or _year(row.get("year")) or None
    if year:
        return f"{clean}|{year}"
    return f"{clean}|in:{str(row.get('category') or '').casefold()}"


def build(path: Path, doc: dict[str, Any], stamp: str) -> None:
    """Write `path` from a parsed snapshot. `stamp` names the snapshot it was built from."""
    started = time.perf_counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(_SCHEMA)
        for kind in KINDS:
            rows = []
            seen: set[str] = set()
            for ord_, row in enumerate(doc.get(kind) or []):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "")
                sort_key = (tvnames.polish(row.get("name"))[0] or name).casefold()
                category = row.get("category") or None
                twin = twin_key(row, ord_)
                rows.append((kind, ord_, name.casefold(), sort_key, _year(row.get("year")), _added(row.get("added")),
                             category, str(category).casefold() if category else None, json.dumps(row),
                             twin, int(twin not in seen)))  # lead: the first copy, for a whole unfiltered list
                seen.add(twin)
            conn.executemany("INSERT INTO titles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        progs = []
        guide = doc.get("guide") or {}
        for chan_ord, channel in enumerate(doc.get("channels") or []):
            key = channel.get("key") if isinstance(channel, dict) else None
            for item_ord, item in enumerate(guide.get(key) or [] if key else []):
                if not isinstance(item, dict):
                    continue
                text = f"{str(item.get('title') or '').casefold()}\n{str(item.get('subtitle') or '').casefold()}"
                progs.append((chan_ord, item_ord, key, text, json.dumps(item)))
        conn.executemany("INSERT INTO progs VALUES (?,?,?,?,?)", progs)
        conn.executemany("INSERT INTO ptext VALUES (?,?,?)", [(p[0], p[1], p[3]) for p in progs])
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("stamp", stamp), ("version", str(VERSION))])
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)
    log.info("catalog index: %d titles, %d programmes in %.1f s",
             sum(len(doc.get(k) or []) for k in KINDS), len(progs), time.perf_counter() - started)


class Reader:
    """Read-only queries, one connection per thread (sqlite3 connections are not shared across threads). A new
    file (renamed into place) is picked up when its stamp changes."""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        mark = (st.st_ino, st.st_mtime_ns)
        held = getattr(self._local, "held", None)
        if held is not None and held[0] == mark:
            return held[1]  # type: ignore[no-any-return]
        if held is not None:
            held[1].close()
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        except sqlite3.Error:
            self._local.held = None
            return None
        self._local.held = (mark, conn)
        return conn

    def stamp(self) -> str | None:
        conn = self._conn()
        if conn is None:
            return None
        try:
            got = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error:
            return None
        return got.get("stamp") if got.get("version") == str(VERSION) else None

    def categories(self) -> set[str]:
        """Every film and series category name (lower-cased) in the index, once per index build."""
        conn = self._conn()
        if conn is None:
            raise sqlite3.OperationalError("no catalog index")
        stamp = self.stamp()
        cached = self.__dict__.get("_categories")
        if cached is not None and cached[0] == stamp:
            return set(cached[1])
        names = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT category_cf FROM titles WHERE category_cf IS NOT NULL AND category_cf != ''")}
        self._categories = (stamp, frozenset(names))
        return names

    def titles(self, kind: str, *, needle: str, category: str | None, kids: set[str] | None, limit: int,
               offset: int, sort: str, skip: Collection[str] | None = None,
               skip_categories: Collection[str] | None = None) -> tuple[list[dict[str, Any]], int, list[str]]:
        """(rows of this page, total matches, category names among the matches). Copies of one title (`twin_key`)
        among the matches count and show once: the first in the provider's order. `kids` limits the rows to those
        categories (lower-cased); None means every row. `skip` is title ids left out before paging (a child's
        level), so `total` and the pages stay right. `skip_categories` (lower-cased) leaves those categories out
        the same way."""
        conn = self._conn()
        if conn is None:
            raise sqlite3.OperationalError("no catalog index")
        where, args = ["kind=?"], [kind]
        if needle:
            where.append("instr(name_cf, ?) > 0")
            args.append(needle)
        if category:
            where.append("category_cf=?")
            args.append(category.casefold())
        if kids is not None:
            if not kids:
                return [], 0, []
            where.append(f"category_cf IN ({','.join('?' * len(kids))})")
            args.extend(sorted(kids))
        if skip_categories:
            where.append("(category_cf IS NULL OR category_cf NOT IN (SELECT value FROM json_each(?)))")
            args.append(json.dumps(sorted(skip_categories)))
        if skip:
            where.append("json_extract(row, '$.id') NOT IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sorted(skip)))
        whole = len(where) == 1  # the whole list: the copies were picked when the index was built
        clause = " AND ".join(where)
        names = [r[0] for r in conn.execute(
            f"SELECT DISTINCT category FROM titles WHERE {clause} AND category IS NOT NULL", args)]  # noqa: S608
        order = SORTS.get(sort, SORTS["name"])
        if whole:
            source = f"titles WHERE {clause} AND lead=1"  # noqa: S608
        else:  # a category, a search or a child's list: the first copy among the matches
            source = (f"(SELECT row, sort_key, ord, year, added, row_number() OVER (PARTITION BY twin ORDER BY ord) "
                      f"AS rn FROM titles WHERE {clause}) WHERE rn=1")  # noqa: S608
        total = conn.execute(f"SELECT count(*) FROM {source}", args).fetchone()[0]  # noqa: S608
        page = conn.execute(f"SELECT row FROM {source} ORDER BY {order} LIMIT ? OFFSET ?",  # noqa: S608
                            [*args, max(0, limit), max(0, offset)]).fetchall()
        return [json.loads(r[0]) for r in page], int(total), sorted(names)

    def programmes(self, needle: str, *, limit: int = 400) -> list[tuple[str, dict[str, Any]]]:
        """(channel key, programme) matching the words, in lineup then guide order."""
        conn = self._conn()
        if conn is None:
            raise sqlite3.OperationalError("no catalog index")
        # The words are scanned in their own narrow table; only the matches' JSON rows are read.
        rows = conn.execute("SELECT p.channel, p.row FROM (SELECT chan_ord, item_ord FROM ptext "
                            "WHERE instr(text_cf, ?) > 0 ORDER BY chan_ord, item_ord LIMIT ?) hit "
                            "JOIN progs p USING (chan_ord, item_ord) ORDER BY p.chan_ord, p.item_ord",
                            [needle, limit]).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]
