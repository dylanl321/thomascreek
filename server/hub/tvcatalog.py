"""IPTV guide, series and movies, cached from the Xtream login the hub already holds.

The phone browses this. It never receives the provider username, password or a
stream URL. Artwork is the provider's own image address when that address has
no credentials in it; otherwise the image is left off.

Channel keys are the same `xc:thomas-creek:<stream_id>` keys the lineup uses.
Movies are `vod:thomas-creek:<stream_id>`, series `series:thomas-creek:<id>`,
episodes `ep:thomas-creek:<id>`.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
import logging
import os
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .artwork import programme_key
from . import catalogdb, iptvaddr, kidratings, tvgroups, tvnames
from .home import iso
from .tvapp import TvAppError
from .tvlineup import LineupError, clean_logo, public_channel

log = logging.getLogger("hub.tvcatalog")
# Category names that say they are for children ("|EN| CHILDREN/FAMILY", "DISNEY+ KIDS", "CLASSIC CARTOONS"):
# the kids' fallback when none of the saved kids' categories is in the provider's lists.
_KIDS_WORDS = re.compile(r"\b(kids?|children|childrens|cartoons?|pixar|junior|nick jr|toddlers?|preschool)\b")

SOURCE = "thomas-creek"
LIST_TTL_S = 6 * 3600
GUIDE_TTL_S = 3 * 3600
GUIDE_RETRY_S = 15 * 60  # an empty guide is tried again sooner than a good one
GUIDE_FORMAT = 2  # bump when the stored guide changes shape, so an old copy is fetched again
DETAIL_TTL_S = 24 * 3600
MAX_GUIDE_BYTES = 512 * 1024 * 1024
GUIDE_TIMEOUT_S = 300
GUIDE_BACK_S = 7 * 86400
GUIDE_AHEAD_S = 36 * 3600
MAX_PROGRAMMES = 60
MAX_AHEAD = 40
MAX_LIST = 100

KidsFn = Callable[[], list[str]]


def _art(url: object) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        return clean_logo(url.strip())
    except LineupError:
        return None


def _first_art(value: object) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return _art(value)


def _id_part(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or "/" in text or ":" in text or "@" in text or len(text) > 80:
        return None
    return text


def channel_key(stream_id: object) -> str | None:
    part = _id_part(stream_id)
    return f"xc:{SOURCE}:{part}" if part else None


def movie_key(stream_id: object) -> str | None:
    part = _id_part(stream_id)
    return f"vod:{SOURCE}:{part}" if part else None


def series_key(series_id: object) -> str | None:
    part = _id_part(series_id)
    return f"series:{SOURCE}:{part}" if part else None


def episode_key(episode_id: object, provider: str = SOURCE) -> str | None:
    part = _id_part(episode_id)
    return f"ep:{provider}:{part}" if part else None


def provider_of(ref: str) -> str | None:
    """The provider in a catalog id (`vod:<provider>:<id>`, `series:...`, `ep:...`)."""
    parts = str(ref or "").split(":")
    return parts[1] if len(parts) == 3 and parts[1] and parts[2] else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xmltv_time(value: str) -> float | None:
    raw = (value or "").strip()
    if len(raw) < 14 or not raw[:14].isdigit():
        return None
    try:
        naive = datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    rest = raw[14:].strip()
    if len(rest) >= 5 and rest[0] in "+-" and rest[1:5].isdigit():
        sign = 1 if rest[0] == "+" else -1
        hours, minutes = int(rest[1:3]), int(rest[3:5])
        return naive.timestamp() - sign * (hours * 3600 + minutes * 60)
    return naive.timestamp()


def _text(parent: ET.Element, name: str) -> str | None:
    for child in parent:
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


_FOLD_LOCK = threading.Lock()
KV_EXTRA_RESULT = "tvcatalog.extra_result"


def _atomic_write(path: Path, text: str) -> None:
    """Write next to the file, then rename over it: the hub and the provider-read child never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


class _file_lock:
    """An exclusive lock on a file, held across processes (fcntl), released on exit."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> "_file_lock":
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a")  # noqa: SIM115 - closed in __exit__
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            self._fh = None
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fh is not None:
            self._fh.close()


class TvCatalog:
    def __init__(self, vault: Any, db: Any, data: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        # Set by the hub: asks the model about titles the rules were unsure of, after each refresh.
        self.settle: Any = None
        self.vault = vault
        self.db = db
        self.path = Path(data) / "tvapp" / "catalog.json"
        self.details_path = Path(data) / "tvapp" / "catalog-details.json"
        self.index = catalogdb.Reader(Path(data) / "tvapp" / "catalog.db")
        self._index_building = threading.Lock()
        self._lineup_building = threading.Lock()
        self._transport = transport
        self.kids_categories: KidsFn = lambda: []
        self._lock = asyncio.Lock()
        # Packed /v1/tv/lineup replies per audience (kids or not), kept by the route with what they were built from.
        self.replies: dict[bool, Any] = {}

    # -- cache -----------------------------------------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """The cached catalog, parsed once per file change. The file holds the whole provider library and guide
        (tens of MB): parsing it on every request took about 15 s per guide call and blocked the hub, so the parsed
        copy is kept while the file's size and change time stay the same. Callers read it and must not change it."""
        try:
            stat = self.path.stat()
        except OSError:
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = getattr(self, "_loaded", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        doc = doc if isinstance(doc, dict) else {}
        self._loaded = (stamp, doc)
        return doc

    def _details(self) -> dict[str, Any]:
        """Every fetched page and episode, parsed once per file change like `load` (the file grows with every
        series opened, and play, Continue watching and art read it on each call). Writers change it and then
        `_write_details` it, which changes the file and so the stamp."""
        try:
            stat = self.details_path.stat()
        except OSError:
            return {"series": {}, "movies": {}, "episodes": {}}
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = getattr(self, "_details_loaded", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            doc = json.loads(self.details_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"series": {}, "movies": {}, "episodes": {}}
        if not isinstance(doc, dict):
            return {"series": {}, "movies": {}, "episodes": {}}
        doc.setdefault("series", {})
        doc.setdefault("movies", {})
        doc.setdefault("episodes", {})
        self._details_loaded = (stamp, doc)
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        _atomic_write(self.path, json.dumps(doc))
        try:
            # Keep the snapshot just written as the parsed copy: parsing tens of MB again on the next read
            # blocked the hub for seconds.
            stat = self.path.stat()
            self._loaded = ((stat.st_mtime_ns, stat.st_size), doc)
        except OSError:
            self._loaded = None
            return
        self._build_index(doc, _stamp_text(self._loaded[0]))

    # -- the SQLite copy for paging and search (hub rebuild phase 3) -------------------------------------------------
    def snapshot_stamp(self) -> str | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return _stamp_text((stat.st_mtime_ns, stat.st_size))

    def _build_index(self, doc: dict[str, Any], stamp: str) -> None:
        with self._index_building:
            if self.index.stamp() == stamp:
                return
            try:
                catalogdb.build(self.index.path, doc, stamp)
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                log.warning("catalog index not built (%s); lists read the snapshot meanwhile", type(exc).__name__)

    def index_ready(self) -> bool:
        """True when catalog.db matches the snapshot. Otherwise one rebuild starts in a worker thread and callers
        read the snapshot as before until it is done."""
        stamp = self.snapshot_stamp()
        if stamp is None:
            return False
        if self.index.stamp() == stamp:
            return True
        if self.__dict__.get("_index_asked") != stamp and not self._index_building.locked():
            self._index_asked = stamp  # one rebuild per snapshot, however many calls ask meanwhile
            self._start_rebuild(stamp)
        return False

    def _start_rebuild(self, stamp: str) -> None:
        threading.Thread(target=self._rebuild_index, args=(stamp,), name="catalog-index", daemon=True).start()

    def _rebuild_index(self, stamp: str) -> None:
        doc = self.load()
        if doc and self.snapshot_stamp() == stamp:
            self._build_index(doc, stamp)

    def _write_details(self, doc: dict[str, Any]) -> None:
        _atomic_write(self.details_path, json.dumps(doc))
        stat = self.details_path.stat()
        self._details_loaded = ((stat.st_mtime_ns, stat.st_size), doc)

    def _overlay(self) -> dict[str, dict[str, Any]]:
        raw = self.db.kv_get("tvapp.lineup") or {}
        rows = raw.get("channels") if isinstance(raw, dict) else None
        out = {}
        for row in rows or []:
            if isinstance(row, dict) and row.get("key"):
                out[str(row["key"])] = row
        return out

    def _art(self, kind: str, key: str | None) -> str | None:
        art = getattr(self, "artwork", None)
        if art is None or not key:
            return None
        return art.public_url(kind, key)

    def _with_title_art(self, row: dict[str, Any], *, show: bool,
                        notes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Adds art addresses. With `notes`, the art notes are collected there and written once by the caller
        (a list of hundreds of titles used to rewrite notes.json once per title and timed the phone out)."""
        out = dict(row)
        ident = out.get("id")
        out["poster_url"] = self._art("poster", ident)
        out["backdrop_url"] = self._art("backdrop", ident)
        art = getattr(self, "artwork", None)
        if art is not None and ident:
            meta = {"title": out.get("name"), "year": out.get("year"), "kind": "tv" if show else "movie"}
            if notes is None:
                art.remember(ident, **meta)
            else:
                notes[ident] = meta
        return out

    def _flush_notes(self, notes: dict[str, dict[str, Any]]) -> None:
        art = getattr(self, "artwork", None)
        if art is not None and notes:
            art.remember_many(notes)

    def _with_programme_art(self, view: dict[str, Any], notes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        art = getattr(self, "artwork", None)
        title = str(view.get("title") or "")
        key = programme_key(title) if art is not None and title else None
        if art is not None and key:
            if notes is None:
                art.remember(key, title=title, kind="tv")
            else:
                notes[key] = {"title": title, "kind": "tv"}
        view["poster_url"] = art.public_url("poster", key) if art is not None and key else None
        view["backdrop_url"] = art.public_url("backdrop", key) if art is not None and key else None
        return view

    def _present_series(self, record: dict[str, Any]) -> dict[str, Any]:
        out = self._with_title_art(_public_series(record), show=True)
        seasons = []
        art = getattr(self, "artwork", None)
        for season in out.get("seasons") or []:
            episodes = []
            for episode in season.get("episodes") or []:
                item = dict(episode)
                item["still_url"] = self._art("still", item.get("id"))
                if art is not None and item.get("id"):
                    art.remember(item["id"], series=out.get("name"), title=out.get("name"),
                                 season=item.get("season"), number=item.get("number"), kind="episode")
                episodes.append(item)
            seasons.append({"number": season.get("number"), "episodes": episodes})
        out["seasons"] = seasons
        return out

    def _present_movie(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._with_title_art(_public_movie(record, detail=True), show=False)

    def _is_kids(self, category: str | None) -> bool:
        if not category:
            return False
        return category.strip().casefold() in self._kid_names()

    def _kid_names(self) -> set[str]:
        """The kids' categories, read once until the database changes. Lists ask this for every row: one read
        per row (tens of thousands for the series list) took the live hub 25 s and more."""
        loaded = self.__dict__.get("_loaded")
        mark = (getattr(getattr(self.db, "_conn", None), "total_changes", None), id(self.kids_categories),
                loaded[0] if loaded else None)
        memo = self.__dict__.get("_kid_memo")
        if mark[0] is not None and memo is not None and memo[0] == mark:
            return memo[1]
        names = {str(c).strip().casefold() for c in self.kids_categories() if str(c).strip()}
        present = self._category_names()
        if present and not names & present:
            # None of the saved kids' categories is in the provider's lists any more (renamed, another provider,
            # or never picked): children got empty pages and searches (Nov 19 2026). Fall back to the provider's
            # categories that say they are for children, until a grown-up picks them on the admin page.
            names = {name for name in present if _KIDS_WORDS.search(name)}
        self._kid_memo = (mark, names)
        return names

    def _category_names(self) -> set[str]:
        """Every category name in the snapshot (films, series, channels), lower-cased, once per snapshot."""
        loaded = self.__dict__.get("_loaded")
        stamp = loaded[0] if loaded else None
        cached = self.__dict__.get("_category_memo")
        if cached is not None and cached[0] == stamp and stamp is not None:
            return cached[1]  # type: ignore[no-any-return]
        doc = self.load()
        out = {str(row.get("category")).strip().casefold() for kind in ("movies", "series", "channels")
               for row in doc.get(kind) or [] if isinstance(row, dict) and row.get("category")}
        loaded = self.__dict__.get("_loaded")
        self._category_memo = (loaded[0] if loaded else None, out)
        return out

    def configured(self) -> bool:
        return all(self.vault.has(n) for n in ("iptv.xtream_server", "iptv.xtream_username", "iptv.xtream_password"))

    def _login(self) -> tuple[str, str, str]:
        """The main provider's login on its best address now (a backup when the main address is marked down)."""
        servers, user, password = self._main_login()
        return servers[0], user, password

    def _main_login(self) -> tuple[list[str], str, str]:
        """The main provider's addresses (best first), username and password."""
        server, user, password = self._main_saved()
        return iptvaddr.ordered(self.db, SOURCE, server), user, password

    def _main_saved(self) -> tuple[str, str, str]:
        server = str(self.vault.get("iptv.xtream_server") or "").strip().rstrip("/")
        user = str(self.vault.get("iptv.xtream_username") or "").strip()
        password = str(self.vault.get("iptv.xtream_password") or "").strip()
        if not server or not user or not password:
            raise TvAppError(409, "not_set_up", "The TV source isn't set up on the hub yet")
        parts = urlsplit(server if "://" in server else "http://" + server)
        if parts.username or parts.password:
            server = urlunsplit((parts.scheme or "http", parts.hostname or "", parts.path, "", ""))
            if parts.port:
                host = f"{parts.hostname}:{parts.port}"
                server = urlunsplit((parts.scheme or "http", host, parts.path, "", ""))
        return server.rstrip("/"), user, password

    async def _download_guide(self, client: httpx.AsyncClient, url: str, params: dict[str, str] | None) -> Path | bytes:
        """Stream the provider's XMLTV to a file next to the catalog. A full guide is far bigger than memory wants."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix="xmltv-", suffix=".xml", dir=self.path.parent)
        os.close(handle)
        target = Path(name)
        size = 0
        try:
            async with client.stream("GET", url, params=params, timeout=GUIDE_TIMEOUT_S) as response:
                if response.status_code != 200:
                    log.info("tv guide: HTTP %s", response.status_code)
                    target.unlink(missing_ok=True)
                    return b""
                with target.open("wb") as out:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_GUIDE_BYTES:
                            log.info("tv guide: larger than %d MB, skipped", MAX_GUIDE_BYTES // (1024 * 1024))
                            out.close()
                            target.unlink(missing_ok=True)
                            return b""
                        out.write(chunk)
        except httpx.HTTPError as exc:
            log.info("tv guide: %s", type(exc).__name__)
            target.unlink(missing_ok=True)
            return b""
        log.info("tv guide: %d MB downloaded", size // (1024 * 1024))
        return target

    # -- build -----------------------------------------------------------------------------------------------------
    def ingest(self, *, live: list[dict[str, Any]] | None = None, live_categories: list[dict[str, Any]] | None = None,
               vod: list[dict[str, Any]] | None = None, vod_categories: list[dict[str, Any]] | None = None,
               series: list[dict[str, Any]] | None = None, series_categories: list[dict[str, Any]] | None = None,
               xmltv: bytes | Path | None = None, now: float | None = None) -> dict[str, Any]:
        """Store a provider snapshot. Tests pass the payloads in; refresh() downloads them."""
        now = time.time() if now is None else now
        live_names = _categories(live_categories)
        vod_names = _categories(vod_categories)
        series_names = _categories(series_categories)
        channels = []
        epg_ids: dict[str, list[str]] = {}
        for row in live or []:
            key = channel_key(row.get("stream_id"))
            if key is None or not isinstance(row, dict):
                continue
            category = live_names.get(str(row.get("category_id") or ""))
            days = _archive_days(row)
            channels.append({
                "key": key, "name": _clip(row.get("name"), 80) or "Channel", "number": _num(row.get("num")),
                "category": category, "logo": _art(row.get("stream_icon")), "catchup_days": days,
                "epg_id": _clip(row.get("epg_channel_id"), 80),
            })
            if channels[-1]["epg_id"]:
                epg_ids.setdefault(str(channels[-1]["epg_id"]), []).append(key)
        movies = [_title(row, movie_key, vod_names, "stream_icon") for row in vod or []]
        shows = [_title(row, series_key, series_names, "cover", id_field="series_id") for row in series or []]
        guide = _parse_xmltv(xmltv, epg_ids, now) if xmltv else {}
        if xmltv:
            log.info("tv guide: %d of %d guide ids matched, %d channels with programmes",
                     sum(1 for keys in epg_ids.values() if any(k in guide for k in keys)), len(epg_ids), len(guide))
        doc = {
            "fetched_at": now,
            "guide_at": (now if guide else now - GUIDE_TTL_S + GUIDE_RETRY_S) if xmltv is not None else 0,
            "guide_format": GUIDE_FORMAT,
            "channels": [c for c in channels if c], "movies": [m for m in movies if m],
            "series": [s for s in shows if s], "guide": guide,
            "categories": {
                "live": sorted({c["category"] for c in channels if c.get("category")}),
                "vod": sorted(set(vod_names.values())),
                "series": sorted(set(series_names.values())),
            },
        }
        self._write(doc)
        self.fold(doc)
        return doc

    # -- other providers ---------------------------------------------------------------------------------------------
    # The main provider (the iptv.xtream_* login) fills the guide, the films and the series. Every other provider
    # added on the admin page is read as a light list (names, ids, file types) and folded into the same catalog, so
    # a title both providers carry gets both copies, and /v1/tv/tune can spread TVs over every provider's logins.
    def _extra_logins(self) -> dict[str, dict[str, Any]]:
        from types import SimpleNamespace

        from . import iptvplay

        shim = SimpleNamespace(config=SimpleNamespace(iptv=None), vault=self.vault, db=self.db)
        out: dict[str, dict[str, Any]] = {}
        for lg in iptvplay.logins(shim):
            pid = str(lg.get("provider") or "")
            if pid == SOURCE or not pid or ":" in pid or "/" in pid or pid in out:
                continue
            secret = str(lg.get("password_secret") or "")
            password = str(self.vault.get(secret) or "").strip() if secret and self.vault.has(secret) else ""
            if lg.get("server") and lg.get("username") and password:
                server = str(lg["server"]).rstrip("/")
                out[pid] = {"server": server, "username": str(lg["username"]), "password": password,
                            "servers": list(lg.get("servers") or [server])}
        return out

    def list_status(self) -> list[dict[str, Any]]:
        """For each provider: how many channels, films and series the hub read from it, when, and why not when a
        read failed or the provider was skipped (Nov 19 2026: one provider never folded with anything, and nothing
        said why). Counts and times only; never a login or an address."""
        from types import SimpleNamespace

        from . import iptvplay

        doc = self.load() or {}
        out = [{"provider": SOURCE, "main": True, "channels": len(doc.get("channels") or []),
                "films": len(doc.get("movies") or []), "series": len(doc.get("series") or []),
                "read_at": doc.get("fetched_at"), "problem": None}]
        readable = self._extra_logins()
        last = self.__dict__.get("_extra_result") or self.db.kv_get(KV_EXTRA_RESULT) or {}
        shim = SimpleNamespace(config=SimpleNamespace(iptv=None), vault=self.vault, db=self.db)
        seen = {SOURCE}
        for lg in iptvplay.logins(shim):
            pid = str(lg.get("provider") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            extra = self.extra(pid) if pid in readable else {}
            problem = None
            if pid not in readable:
                problem = "skipped: no address, username or saved password for any of its logins"
            elif str(last.get(pid) or "").startswith("not read"):
                why = str(last[pid])[len("not read"):].lstrip(": ")
                problem = f"the last read failed: {why}" if why else "the last read failed (see the hub log for the reason)"
            elif str(last.get(pid) or "").startswith("partly read"):
                problem = str(last[pid]).split(" (kept the last copy)", 1)[0] + " (kept the last copy)"
            elif not extra:
                problem = "not read yet"
            out.append({"provider": pid, "main": False, "channels": len(extra.get("channels") or []),
                        "films": len(extra.get("movies") or []), "series": len(extra.get("series") or []),
                        "read_at": extra.get("fetched_at"), "problem": problem})
        return out

    def _extra_path(self, pid: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pid)[:60]
        return self.path.parent / f"extra-{safe}.json"

    def extra(self, pid: str) -> dict[str, Any]:
        path = self._extra_path(pid)
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return {}
        cache = self.__dict__.setdefault("_extra_cache", {})
        hit = cache.get(pid)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        cache[pid] = (stamp, doc)
        return doc

    def extra_movie(self, ref: str) -> dict[str, Any] | None:
        """A film from another provider (`vod:<provider>:<id>`): its name and file type, for playback."""
        return self.extra_row("movies", ref)

    def extra_row(self, bucket: str, ref: str) -> dict[str, Any] | None:
        """A film or series from another provider's light list. None for the main provider's ids."""
        prefix = {"movies": "vod", "series": "series"}.get(bucket)
        parts = str(ref or "").split(":")
        if prefix is None or len(parts) != 3 or parts[0] != prefix or parts[1] == SOURCE:
            return None
        return next((row for row in self.extra(parts[1]).get(bucket) or [] if row.get("id") == ref), None)

    def known_row(self, bucket: str, ref: str) -> dict[str, Any] | None:
        """A film or series row from any provider: the main list first, then the provider the id names."""
        doc = self.load()
        ids = getattr(self, "_ids", None)
        if ids is None or ids[0] is not doc:
            # One id map per loaded file, instead of a scan of every film or series on each call.
            ids = (doc, {name: {str(item.get("id")): item for item in doc.get(name) or [] if isinstance(item, dict)}
                         for name in ("movies", "series")})
            self._ids = ids
        row = ids[1].get(bucket, {}).get(ref) if bucket in ids[1] else next(
            (item for item in doc.get(bucket) or [] if item.get("id") == ref), None)
        return row if row is not None else self.extra_row(bucket, ref)

    def _login_for(self, ref: str) -> tuple[str, list[str], str, str]:
        """The login that can open this id's page: the main one, or the other provider's own.
        (provider id, its addresses best first, username, password)."""
        pid = provider_of(ref)
        if pid in (None, SOURCE):
            servers, user, password = self._main_login()
            return SOURCE, servers, user, password
        lg = self._extra_logins().get(pid)
        if lg is None:
            raise TvAppError(404, "not_found", "That provider isn't set up on the hub any more")
        return str(pid), lg["servers"], lg["username"], lg["password"]

    async def _api(self, pid: str, servers: list[str], params: dict[str, str],
                   wait_s: float = 45) -> tuple[str, httpx.Response]:
        """One player_api.php call, on the first of the provider's addresses that answers. An address that can't be
        reached (or answers with a redirect or a server error, as a blocked domain does) is marked down and the
        next one is tried."""
        last = "The TV provider didn't answer"
        for server in servers:
            try:
                async with httpx.AsyncClient(timeout=wait_s, follow_redirects=False,
                                             transport=self._transport) as client:
                    response = await client.get(f"{server}/player_api.php", params=params)
            except httpx.TimeoutException:
                iptvaddr.note(self.db, pid, server, False, "timed out")
                last = "The TV provider timed out"
                continue
            except httpx.HTTPError:
                iptvaddr.note(self.db, pid, server, False, "couldn't be reached")
                continue
            if response.status_code in (301, 302, 303, 307, 308) or response.status_code >= 500:
                iptvaddr.note(self.db, pid, server, False, f"answered {response.status_code}")
                last = ("The TV provider redirected. Check the server address."
                        if response.status_code < 400 else last)
                continue
            iptvaddr.note(self.db, pid, server, True)
            return server, response
        raise TvAppError(502, "bad_provider", last)

    def ingest_extra(self, pid: str, *, live: list[Any] | None, vod: list[Any] | None, series: list[Any] | None,
                     now: float | None = None, keep: dict[str, Any] | None = None) -> dict[str, Any]:
        """`keep` is the provider's last saved doc: a list passed as None (its read failed) keeps that copy."""
        def key(prefix: str, value: object) -> str | None:
            part = _id_part(value)
            return f"{prefix}:{pid}:{part}" if part else None

        channels, movies, shows = [], [], []
        for row in live or []:
            if isinstance(row, dict) and (k := key("xc", row.get("stream_id"))):
                channels.append({"key": k, "name": _clip(row.get("name"), 80) or "Channel",
                                 "epg_id": _clip(row.get("epg_channel_id"), 80)})
        for rows, out, prefix, id_field in ((vod, movies, "vod", "stream_id"), (series, shows, "series", "series_id")):
            for row in rows or []:
                if not isinstance(row, dict) or not (k := key(prefix, row.get(id_field))):
                    continue
                item = {"id": k, "name": _clip(row.get("name") or row.get("title"), 120) or "Untitled",
                        "year": _clip(row.get("year") or row.get("releaseDate"), 20)}
                tmdb = _tmdb_id(row.get("tmdb") or row.get("tmdb_id"))
                if tmdb:
                    item["tmdb"] = tmdb
                ext = _ext(row.get("container_extension"))
                if ext:
                    item["ext"] = ext
                out.append(item)
        if keep:
            channels = channels if live is not None else list(keep.get("channels") or [])
            movies = movies if vod is not None else list(keep.get("movies") or [])
            shows = shows if series is not None else list(keep.get("series") or [])
        doc = {"provider": pid, "fetched_at": time.time() if now is None else now,
               "channels": channels, "movies": movies, "series": shows}
        path = self._extra_path(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(path)
        return doc

    def fold(self, doc: dict[str, Any] | None = None) -> None:
        """Fold every provider's titles into the one catalog. All providers go in one call: a source left out
        of a call is treated as gone."""
        with _FOLD_LOCK:  # one fold at a time: the main refresh and the other providers' read can overlap
            with _file_lock(self.path.parent / "fold.lock"):  # also across the hub and its provider-read child
                self._fold(doc)

    def _fold(self, doc: dict[str, Any] | None) -> None:
        try:
            from .iptvcanon import observe, sources_from_catalog

            doc = self.load() if doc is None else doc
            sources = sources_from_catalog(doc) if doc else []
            for pid in self._extra_logins():
                extra = self.extra(pid)
                if extra:
                    sources += sources_from_catalog(extra, pid)
            observe(self.db, sources)
        except Exception as exc:  # noqa: BLE001
            log.info("catalog fold: %s", exc)

    def _refresh_soon(self) -> None:
        """One background `refresh`, unless one is already running. A failure is noted; the old lists stay."""
        if self.__dict__.get("_refresh_running") or self._lock.locked():
            return

        async def run() -> None:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001 - the stale lists keep serving; the next call tries again
                log.warning("tv catalog: background refresh failed (%s)", type(exc).__name__)
            finally:
                self._refresh_running = False

        try:
            self._refresh_task = asyncio.get_running_loop().create_task(run())
            self._refresh_running = True
        except RuntimeError:
            pass

    def _extras_due(self, now: float) -> None:
        """Read another provider's lists in the background when they are missing or older than the main list's
        refresh time (a provider just added, or a hub that restarted with a fresh main list)."""
        if self.__dict__.get("_extras_running"):
            return
        try:
            due = any(now - float(self.extra(pid).get("fetched_at") or 0) >= LIST_TTL_S for pid in self._extra_logins())
        except Exception:  # noqa: BLE001
            return
        if not due:
            return

        async def run() -> None:
            try:
                await self.refresh_extras()
            finally:
                self._extras_running = False

        try:
            asyncio.get_running_loop().create_task(run())
            self._extras_running = True
        except RuntimeError:
            pass

    async def refresh_extras(self) -> dict[str, str]:
        """Read every other provider's lists. One provider failing keeps its last list and doesn't stop the rest.
        Under `hub serve` the reading runs in a child process (catalogjob.py) so it never stalls a request."""
        from . import catalogjob

        if not catalogjob.enabled():
            return await self.refresh_extras_here()
        if not self._extra_logins():
            return {}
        got = await catalogjob.run("extras")
        self._extra_result = got.get("extras") or {}
        return self._extra_result

    async def refresh_extras_here(self) -> dict[str, str]:
        """Read each other provider's three lists one at a time. A list that fails keeps its last copy, and the
        result says which list failed and why in a few words (never an address or a login), so the providers sheet
        can show it (Nov 19 2026: one provider never folded and nothing said why)."""
        result: dict[str, str] = {}
        logins = self._extra_logins()
        names = {"get_live_streams": "channels", "get_vod_streams": "films", "get_series": "series"}
        for pid, lg in logins.items():
            params = {"username": lg["username"], "password": lg["password"]}
            servers = list(lg["servers"])
            got: list[list[Any] | None] = []
            failed: list[str] = []
            for action in names:
                try:
                    used, r = await self._api(pid, servers, {**params, "action": action}, wait_s=EXTRA_WAIT_S)
                    # Stay on the address that just answered for the rest of this read.
                    servers = [used] + [s for s in servers if s != used]
                    part = await asyncio.to_thread(_list_body, r)
                    got.append(part)
                except (httpx.HTTPError, TvAppError, ValueError) as exc:
                    got.append(None)
                    failed.append(f"{names[action]} {_read_reason(exc)}")
            if len(failed) == len(names):
                result[pid] = "not read: " + "; ".join(sorted(set(f.split(" ", 1)[1] for f in failed)))
                log.info("provider %s: lists not read (%s)", pid, result[pid][10:])
                continue
            try:
                doc = await asyncio.to_thread(self.ingest_extra, pid, live=got[0], vod=got[1], series=got[2],
                                              keep=self.extra(pid) if failed else None)
            except (OSError, ValueError) as exc:
                result[pid] = f"not read: couldn't be saved ({type(exc).__name__})"
                continue
            counts = f"{len(doc['channels'])} channels, {len(doc['movies'])} films, {len(doc['series'])} series"
            result[pid] = f"partly read: {'; '.join(failed)} (kept the last copy); {counts}" if failed else counts
            log.info("provider %s: %s", pid, result[pid])
        self._extra_result = result
        try:
            self.db.kv_set(KV_EXTRA_RESULT, result)  # list_status reads it in the hub when a child did the read
        except Exception:  # noqa: BLE001
            pass
        if logins:
            await asyncio.to_thread(self.fold)
        return result

    def store_series(self, payload: dict[str, Any], series_id: str) -> dict[str, Any]:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        episodes_in = payload.get("episodes") if isinstance(payload.get("episodes"), dict) else {}
        known = self.known_row("series", series_id) or {}
        kids = self._is_kids(known.get("category"))
        provider = provider_of(series_id) or SOURCE
        seasons = []
        details = self._details()
        for season_key, episodes in episodes_in.items():
            if not isinstance(episodes, list):
                continue
            try:
                season_no = int(season_key)
            except (TypeError, ValueError):
                continue
            shown = []
            for ep in episodes:
                if not isinstance(ep, dict):
                    continue
                eid = episode_key(ep.get("id"), provider)
                if eid is None:
                    continue
                meta = ep.get("info") if isinstance(ep.get("info"), dict) else {}
                item = {
                    "id": eid, "season": season_no, "number": _num(ep.get("episode_num")),
                    "title": _clip(ep.get("title"), 120) or f"Episode {ep.get('episode_num') or ''}".strip(),
                    "plot": _clip(meta.get("plot"), 400), "still": _art(meta.get("movie_image")),
                    "duration_s": _duration(meta.get("duration_secs"), meta.get("duration")),
                }
                shown.append(item)
                details["episodes"][eid] = {
                    "id": eid, "series_id": series_id, "title": item["title"], "kids": kids,
                    "series_name": known.get("name"), "season": season_no, "number": item["number"],
                    "ext": _ext(ep.get("container_extension")), "fetched_at": time.time(),
                }
            shown.sort(key=lambda item: (item["number"] is None, item["number"] or 0))
            if shown:
                seasons.append({"number": season_no, "episodes": shown})
        seasons.sort(key=lambda season: season["number"])
        record = {
            "id": series_id, "name": known.get("name") or _clip(info.get("name"), 120),
            "cover": _art(info.get("cover")) or known.get("cover"),
            "backdrop": _first_art(info.get("backdrop_path")),
            "plot": _clip(info.get("plot"), 600), "genre": _clip(info.get("genre"), 80),
            "year": _clip(info.get("releaseDate") or info.get("year"), 20),
            "category": known.get("category"), "kids": kids, "seasons": seasons, "fetched_at": time.time(),
        }
        details["series"][series_id] = record
        self._write_details(details)
        return self._present_series(record)

    def store_movie(self, payload: dict[str, Any], movie_id: str) -> dict[str, Any]:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        known = self.known_row("movies", movie_id) or {}
        record = {
            "id": movie_id, "name": known.get("name") or _clip(info.get("name"), 120),
            "cover": _art(info.get("movie_image")) or known.get("cover"),
            "backdrop": _first_art(info.get("backdrop_path")),
            "plot": _clip(info.get("plot"), 600), "genre": _clip(info.get("genre"), 80),
            "year": _clip(info.get("releasedate") or info.get("year"), 20),
            "duration_s": _duration(info.get("duration_secs"), info.get("duration")),
            "category": known.get("category"), "kids": self._is_kids(known.get("category")),
            "fetched_at": time.time(),
        }
        details = self._details()
        details["movies"][movie_id] = record
        self._write_details(details)
        return self._present_movie(record)

    # -- fetch -----------------------------------------------------------------------------------------------------
    async def ensure(self) -> dict[str, Any]:
        doc = self.load()
        now = time.time()
        lists_ok = bool(doc) and now - float(doc.get("fetched_at") or 0) < LIST_TTL_S
        guide_ok = (now - float(doc.get("guide_at") or 0) < GUIDE_TTL_S
                    and doc.get("guide_format") == GUIDE_FORMAT)
        if lists_ok and guide_ok:
            self._extras_due(now)
            return doc
        if not self.configured():
            if doc:
                return doc
            raise TvAppError(409, "not_set_up", "The TV source isn't set up on the hub yet")
        if doc and doc.get("guide_format") == GUIDE_FORMAT:
            # Stale but usable: answer from it now and read the provider in the background. Before, the first call
            # after the lists or guide aged out (a play, a search, a page) waited for the whole provider download
            # and timed the phone out ("finding a free login", Nov 19 2026).
            self._refresh_soon()
            return doc
        try:
            await self.refresh()
        except TvAppError:
            if doc:
                return doc
            raise
        return self.load()

    async def refresh(self) -> dict[str, Any]:
        """Read the main provider's lists and guide, then every other provider's, then settle unsure matches.
        Under `hub serve` the reading runs in a child process (catalogjob.py); the hub answers from the last
        snapshot meanwhile and parses the new one off the loop when the child is done."""
        from . import catalogjob

        if not catalogjob.enabled():
            return await self.refresh_here()
        async with self._lock:
            got = await catalogjob.run("refresh")
        self._extra_result = got.get("extras") or {}
        doc = await asyncio.to_thread(self.load)
        await self._settle()
        return doc

    async def _settle(self) -> None:
        if self.settle is not None:
            try:
                await self.settle()
            except Exception as exc:  # noqa: BLE001
                log.info("catalog match: %s", type(exc).__name__)

    async def refresh_here(self, *, settle: bool = True) -> dict[str, Any]:
        async with self._lock:
            servers, user, password = self._main_login()
            params = {"username": user, "password": password}
            # The first address that answers the first list read is used for the whole refresh; one that doesn't
            # is marked down (a blocked domain) and the next backup address is tried.
            server, first = await self._api(SOURCE, servers, {**params, "action": "get_live_streams"})
            async with httpx.AsyncClient(timeout=45, follow_redirects=False, transport=self._transport) as client:
                async def pull(action: str) -> Any:
                    try:
                        response = await client.get(f"{server}/player_api.php", params={**params, "action": action})
                    except httpx.HTTPError:
                        raise TvAppError(502, "bad_provider", "The TV provider didn't answer") from None
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise TvAppError(502, "bad_provider", "The TV provider redirected. Check the server address.")
                    if response.status_code >= 400:
                        raise TvAppError(502, "bad_provider", "The TV provider didn't answer")
                    try:
                        return await asyncio.to_thread(response.json)
                    except json.JSONDecodeError:
                        raise TvAppError(502, "bad_provider", "The TV provider sent something that isn't a list") from None

                if first.status_code >= 400:
                    raise TvAppError(502, "bad_provider", "The TV provider didn't answer")
                try:
                    live = await asyncio.to_thread(first.json)
                except json.JSONDecodeError:
                    raise TvAppError(502, "bad_provider", "The TV provider sent something that isn't a list") from None
                live_categories = await pull("get_live_categories")
                vod = await pull("get_vod_streams")
                vod_categories = await pull("get_vod_categories")
                series = await pull("get_series")
                series_categories = await pull("get_series_categories")
                guide_url = str(self.vault.get("iptv.epg_url") or "").strip() or f"{server}/xmltv.php"
                xmltv = await self._download_guide(client, guide_url, None if self.vault.get("iptv.epg_url") else params)
            if not all(isinstance(part, list) for part in (live, live_categories, vod, vod_categories, series, series_categories)):
                raise TvAppError(502, "bad_provider", "The TV provider didn't send its lists")
            try:
                # Parsing the guide, writing the snapshot and folding every provider's titles take from seconds to
                # minutes with a large provider. On the hub's loop that stopped it answering anyone (Nov 19), so
                # it runs in a worker thread.
                doc = await asyncio.to_thread(
                    lambda: self.ingest(live=live, live_categories=live_categories, vod=vod,
                                        vod_categories=vod_categories, series=series,
                                        series_categories=series_categories, xmltv=xmltv))
            finally:
                if isinstance(xmltv, Path):
                    xmltv.unlink(missing_ok=True)
        await self.refresh_extras_here()
        if settle:
            await self._settle()
        return doc

    async def fetch_series(self, series_id: str) -> dict[str, Any]:
        cached = self._details()["series"].get(series_id)
        if cached and time.time() - float(cached.get("fetched_at") or 0) < DETAIL_TTL_S:
            return self._present_series(cached)
        pid, servers, user, password = self._login_for(series_id)
        part = series_id.split(":")[-1]
        _used, response = await self._api(pid, servers, {
            "username": user, "password": password, "action": "get_series_info", "series_id": part})
        if response.status_code >= 400:
            raise TvAppError(502, "bad_provider", "The TV provider didn't send that series")
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise TvAppError(502, "bad_provider", "The TV provider didn't send that series") from None
        if not isinstance(payload, dict):
            raise TvAppError(404, "not_found", "That series isn't in the library")
        return self.store_series(payload, series_id)

    async def fetch_movie(self, movie_id: str) -> dict[str, Any]:
        cached = self._details()["movies"].get(movie_id)
        if cached and time.time() - float(cached.get("fetched_at") or 0) < DETAIL_TTL_S:
            return self._present_movie(cached)
        pid, servers, user, password = self._login_for(movie_id)
        part = movie_id.split(":")[-1]
        _used, response = await self._api(pid, servers, {
            "username": user, "password": password, "action": "get_vod_info", "vod_id": part})
        if response.status_code >= 400:
            raise TvAppError(502, "bad_provider", "The TV provider didn't send that movie")
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise TvAppError(502, "bad_provider", "The TV provider didn't send that movie") from None
        if not isinstance(payload, dict):
            raise TvAppError(404, "not_found", "That movie isn't in the library")
        return self.store_movie(payload, movie_id)

    # -- reads -----------------------------------------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        doc = self.load()
        return {
            "fetched_at": iso(doc.get("fetched_at")), "guide_at": iso(doc.get("guide_at")) if doc.get("guide_at") else None,
            "channels": len(doc.get("channels") or []), "movies": len(doc.get("movies") or []),
            "series": len(doc.get("series") or []), "configured": self.configured(),
        }

    def _kv_stamp(self, key: str) -> Any:
        """A cheap change mark for a setting: its write time, or its value where the store has no times."""
        stamp = getattr(self.db, "kv_stamp", None)
        if callable(stamp):
            return stamp(key)
        return json.dumps(self.db.kv_get(key), sort_keys=True, default=str)

    def _lineup_stamp(self, kids_only: bool) -> Any:
        """What the built lineup depends on, read without parsing the catalog or the large canonical-id map
        (decoding that map on every call made each lineup and groups call take seconds on the live hub)."""
        from .iptvcanon import KV
        loaded = getattr(self, "_loaded", None)
        try:
            stat = self.path.stat()
            file_stamp: Any = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_stamp = None
        if loaded is None or loaded[0] != file_stamp:
            file_stamp = (file_stamp, "unloaded")
        return (file_stamp, self._kv_stamp("tvapp.lineup"), self._kv_stamp(KV), kids_only, int(time.time() // 600))

    def _lineup_body(self, kids_only: bool) -> tuple[Any, dict[str, Any] | None]:
        """The built lineup shared by every caller (not to be changed) and its stamp."""
        doc = self.load()
        if not doc.get("channels"):
            return None, None
        stamp = self._lineup_stamp(kids_only)
        cache: dict[bool, tuple[Any, dict[str, Any]]] | None = getattr(self, "_lineup_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._lineup_cache = cache
        hit = cache.get(kids_only)
        if hit is None or hit[0] != stamp:
            with self._lineup_building:  # callers run in worker threads now: one build per change, not one each
                hit = cache.get(kids_only)
                if hit is None or hit[0] != stamp:
                    started = time.monotonic()
                    body = self._build_lineup(doc, self._overlay(), self._live_canon(), kids_only=kids_only)
                    hit = cache[kids_only] = (stamp, body)
                    log.info("live lineup built: %d channels in %.2fs", len(body["channels"]),
                             time.monotonic() - started)
        return hit

    def lineup(self, *, kids_only: bool) -> dict[str, Any] | None:
        """The live lineup, worked out once per snapshot and edit (17k channels took about 6 s a call). Each
        caller gets its own row copies, since routes add health to them."""
        _, body = self._lineup_body(kids_only)
        if body is None:
            return None
        return {**body, "channels": [dict(row) for row in body["channels"]]}

    def lineup_stamp(self, kids_only: bool) -> Any:
        """The stamp of the lineup `lineup()` returns now, for callers that keep their own copy of a reply."""
        return self._lineup_body(kids_only)[0]

    def groups(self, *, kids_only: bool) -> dict[str, Any]:
        """Live categories by country and kind (tvgroups), worked out once per lineup."""
        stamp, body = self._lineup_body(kids_only)
        cache: dict[bool, tuple[Any, dict[str, Any]]] | None = getattr(self, "_groups_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._groups_cache = cache
        hit = cache.get(kids_only)
        if hit is None or hit[0] != stamp or stamp is None:
            lineup = body or {"version": 0, "channels": []}
            built = {"version": lineup.get("version") or 0, **tvgroups.groups(lineup.get("channels") or [])}
            hit = cache[kids_only] = (stamp, built)
        return hit[1]

    def _build_lineup(self, doc: dict[str, Any], overlay: dict[str, dict[str, Any]], canon: dict[str, str], *,
                      kids_only: bool) -> dict[str, Any]:
        rows = []
        seen: set[str] = set()
        for row in doc["channels"]:
            shown = self._present_channel(row, overlay)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            if tvnames.is_divider(shown.get("name")):
                continue  # the provider's "###### ARABIC ######" section headers are not channels
            public = {key: shown[key] for key in ("key", "name", "display_name", "number", "group", "logo", "kids")}
            if shown.get("catchup_days"):
                public["catchup_days"] = shown["catchup_days"]
            public["logo_url"] = self._art("logo", shown["key"])
            if shown.get("group"):
                # The provider's category made readable once here ("|US| SPORTS" as "Sports"); screens show it.
                public["group_label"] = tvnames.category(shown["group"])
                place = tvgroups.country_of(shown["group"])
                if place:
                    public["group_country"] = place[0]
            _with_canon(public, canon)
            rows.append(public)
            seen.add(shown["key"])
        for key, over in overlay.items():
            if key in seen or over.get("hidden"):
                continue
            if kids_only and not over.get("kids"):
                continue
            extra = public_channel(over)
            extra["logo_url"] = self._art("logo", extra["key"])
            _with_canon(extra, canon)
            rows.append(extra)
        rows.sort(key=lambda row: (row.get("number") is None, row.get("number") or 0, str(row.get("name") or "").casefold()))
        return {"version": int(doc.get("fetched_at") or 0), "channels": rows}

    def _live_canon(self) -> dict[str, str]:
        """Folded catalog ids by source (`provider|live|stream_id`). POST /v1/tv/tune takes these."""
        try:
            from .iptvcanon import index
            return index(self.db).by_source  # read once per change of the folded catalog, not per lineup
        except Exception as exc:  # noqa: BLE001
            log.info("lineup canonical ids: %s", exc)
            return {}

    def market_counts(self) -> dict[str, int]:
        """Local stations the provider carries per market (tvbasic.MARKETS), worked out once per snapshot."""
        from . import tvbasic
        doc = self.load()
        stamp = (doc.get("fetched_at"), tvbasic.VERSION)
        cached = getattr(self, "_market_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        texts: list[tuple[str, str]] = []
        for row in doc.get("channels") or []:
            name = str(row.get("name") or "")
            group = str(row.get("group") or row.get("category") or "")
            if tvbasic.is_us({"name": name, "category": group}):
                text = tvbasic.clean(name)
                texts.append((text, text + " " + group.casefold()))
        counts = tvbasic.locals_per_market(texts)
        self._market_cache = (stamp, counts)
        return counts

    def basic(self, home: tuple[float, float] | None) -> dict[str, Any] | None:
        """The household's basic cable lineup (see tvbasic). Worked out once per provider snapshot."""
        from . import tvbasic
        doc = self.load()
        if not doc.get("channels"):
            return None
        stamp = (doc.get("fetched_at"), doc.get("guide_at"), tvbasic.VERSION, len(self._overlay()))
        # One lineup per place (the house, and each market a phone picked), kept for this snapshot.
        if getattr(self, "_basic_stamp", None) != stamp:
            self._basic_stamp = stamp
            self._basic_by_home: dict[Any, dict[str, Any]] = {}
        by_home = self._basic_by_home
        if home in by_home:
            return by_home[home]
        rows = []
        overlay = self._overlay()
        for row in doc["channels"]:
            shown = self._present_channel(row, overlay)
            if shown is not None:
                rows.append({"key": shown["key"], "name": shown["name"], "category": shown["group"]})
        guide = doc.get("guide") or {}
        out = tvbasic.build(rows, home, has_guide=lambda key: bool(guide.get(key)))
        if len(by_home) >= 16:
            by_home.pop(next(iter(by_home)))
        by_home[home] = out
        return out

    def channel(self, key: str) -> dict[str, Any] | None:
        for row in self.load().get("channels") or []:
            if row.get("key") == key:
                return self._present_channel(row)
        return None

    def _present_channel(self, row: dict[str, Any],
                         overlay: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """`overlay` is the lineup edits, read once by a caller that loops over every channel."""
        over = (self._overlay() if overlay is None else overlay).get(row["key"]) or {}
        if over.get("hidden"):
            return None
        number = over.get("number") if over.get("number") is not None else row.get("number")
        return {
            "key": row["key"],
            "name": over.get("name") or row.get("name"),
            # Optional, for the screens: a name set on the guide editor as typed, else the provider's cleaned
            # ("US| THE OFFICE FHD" as "The Office"). `name` stays as before.
            "display_name": over.get("name") or tvnames.channel_name(row.get("name")),
            "number": number,
            "group": over.get("group") or row.get("category"),
            "logo": over.get("logo") or row.get("logo"),
            # A grown-up's own kids switch wins; otherwise a kids' category, minus the grown-up cartoons
            # providers file there ("Adult Swim", "South Park").
            "kids": bool(over.get("kids")) or (self._is_kids(row.get("category"))
                                               and not kidratings.denied_channel(row.get("name"))),
            "catchup_days": int(row.get("catchup_days") or 0),
            "category": row.get("category"),
        }

    def guide(self, *, start: float, end: float, channel: str | None, kids_only: bool, now: float | None = None,
              channels: list[str] | None = None) -> dict[str, Any]:
        """`channel` asks for one channel, `channels` for several in one call (the TVs used to ask one at a time)."""
        now = time.time() if now is None else now
        picked = set(channels or []) | ({channel} if channel else set())
        doc = self.load()
        overlay = self._overlay()
        notes: dict[str, dict[str, Any]] = {}
        wanted = []
        for row in doc.get("channels") or []:
            if len(wanted) >= 200:
                break
            if picked and row.get("key") not in picked:
                continue
            shown = self._present_channel(row, overlay)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            programmes = []
            current = None
            nxt = None
            for item in (doc.get("guide") or {}).get(shown["key"]) or []:
                begins, finishes = float(item["start_ts"]), float(item["end_ts"])
                if finishes <= min(start, now) or (begins >= end and nxt is not None):
                    continue
                view = self._with_programme_art(_programme(item, shown, now), notes)
                if begins <= now < finishes and current is None:
                    current = view
                elif begins >= now and nxt is None:
                    nxt = view
                if finishes > start and begins < end:
                    programmes.append(view)
            if picked or programmes or current:
                wanted.append({
                    "key": shown["key"], "name": shown["name"], "number": shown["number"],
                    "group": shown["group"], "logo": shown["logo"], "logo_url": self._art("logo", shown["key"]),
                    "kids": shown["kids"],
                    "catchup_days": shown["catchup_days"] or None, "now": current, "next": nxt,
                    "programmes": programmes[:MAX_PROGRAMMES],
                })
        self._flush_notes(notes)
        return {"from": iso(start), "to": iso(end), "channels": wanted[:200]}

    def movies(self, *, q: str | None, category: str | None, kids_only: bool, limit: int, offset: int = 0,
               sort: str = "name") -> dict[str, Any]:
        return self._list("movies", q=q, category=category, kids_only=kids_only, limit=limit, offset=offset, sort=sort)

    def series(self, *, q: str | None, category: str | None, kids_only: bool, limit: int, offset: int = 0,
               sort: str = "name") -> dict[str, Any]:
        return self._list("series", q=q, category=category, kids_only=kids_only, limit=limit, offset=offset, sort=sort)

    def _list(self, kind: str, *, q: str | None, category: str | None, kids_only: bool, limit: int, offset: int = 0,
              sort: str = "name") -> dict[str, Any]:
        """One page of the list. `sort`: `name` (A to Z), `year` (newest first) or `added` (last added first; the
        provider's own order where it sends no date). `total` counts every match, for paging."""
        needle = (q or "").strip().casefold()
        if self.index_ready():
            try:
                rows, total, names_found = self.index.titles(
                    kind, needle=needle, category=category, kids=self._kid_names() if kids_only else None,
                    limit=limit, offset=offset, sort=sort)
            except sqlite3.Error as exc:
                log.info("catalog index read failed (%s); reading the snapshot", type(exc).__name__)
            else:
                return self._page(kind, [(row, self._is_kids(row.get("category"))) for row in rows],
                                  names_found, total, max(0, offset))
        found: list[tuple[dict[str, Any], bool]] = []
        names: set[str] = set()
        for row in self.load().get(kind) or []:  # the provider's order: the index is the "added" fallback
            kids = self._is_kids(row.get("category"))
            if kids_only and not kids:
                continue
            if category and (row.get("category") or "").casefold() != category.casefold():
                continue
            if needle and needle not in str(row.get("name") or "").casefold():
                continue
            if row.get("category"):
                names.add(row["category"])
            found.append((row, kids))
        # Sorted on the readable name ("Dune", not "EN - Dune (2021)"); only the page is turned into public rows.
        titles = [(tvnames.polish(row.get("name"))[0] or str(row.get("name") or "")).casefold() for row, _ in found]
        by_name = sorted(range(len(found)), key=lambda i: titles[i])
        if sort == "year":
            order = sorted(by_name, key=lambda i: -(_year_of(found[i][0].get("year")) or 0))
        elif sort == "added":
            order = sorted(range(len(found)), key=lambda i: (-(found[i][0].get("added") or 0), -i))
        else:
            order = by_name

        start = max(0, offset)
        return self._page(kind, [found[i] for i in order[start:start + limit]], sorted(names), len(found), start)

    def _page(self, kind: str, rows: list[tuple[dict[str, Any], bool]], names: list[str], total: int,
              start: int) -> dict[str, Any]:
        def public(row: dict[str, Any], kids: bool) -> dict[str, Any]:
            item = dict(row)
            item["kids"] = kids
            item.pop("category_id", None)
            return _public_movie(item) if kind == "movies" else _public_series_row(item)

        label = "movies" if kind == "movies" else "series"
        page = [public(row, kids) for row, kids in rows]
        # Art only for the rows sent back, noted in one write.
        notes: dict[str, dict[str, Any]] = {}
        shown = [self._with_title_art(row, show=kind != "movies", notes=notes) for row in page]
        self._flush_notes(notes)
        return {"categories": names, label: shown, "total": total, "offset": start}

    def movie(self, movie_id: str) -> dict[str, Any] | None:
        if not movie_id.startswith("vod:"):
            return None
        row = self.known_row("movies", movie_id)
        if row is None:
            return None
        detail = self._details()["movies"].get(movie_id) or {}
        merged = {**row, **{k: v for k, v in detail.items() if v is not None}}
        merged["kids"] = self._is_kids(row.get("category"))
        return merged

    def episode(self, episode_id: str) -> dict[str, Any] | None:
        if not episode_id.startswith("ep:"):
            return None
        row = self._details()["episodes"].get(episode_id)
        if not row:
            return None
        series = self.known_row("series", str(row.get("series_id") or "")) or {}
        kids = self._is_kids(series.get("category"))
        return {**row, "kids": kids, "series_name": series.get("name") or row.get("series_name")}

    def search(self, q: str, *, kids_only: bool, now: float | None = None,
               timing: dict[str, float] | None = None) -> dict[str, Any]:
        """`timing` (optional) is filled with the milliseconds each part took, for the route's Server-Timing."""
        needle = q.strip().casefold()
        now = time.time() if now is None else now
        clock = time.perf_counter()
        marks = timing if timing is not None else {}

        def lap(name: str) -> None:
            nonlocal clock
            at = time.perf_counter()
            marks[name] = round((at - clock) * 1000, 1)
            clock = at

        channels: list[dict[str, Any]] = []
        programmes: list[dict[str, Any]] = []
        doc = self.load()
        overlay = self._overlay()
        lap("load")
        notes: dict[str, dict[str, Any]] = {}
        guide = doc.get("guide") or {}
        indexed = self.index_ready()
        by_key: dict[str, dict[str, Any]] = {}
        for row in doc.get("channels") or []:
            shown = self._present_channel(row, overlay)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            by_key[shown["key"]] = shown
            if needle in str(shown["name"]).casefold():
                card = {key: shown[key] for key in ("key", "name", "display_name", "number", "group", "logo", "kids")}
                card["logo_url"] = self._art("logo", shown["key"])
                channels.append(card)
            if indexed or len(programmes) >= 20:
                continue
            for item in guide.get(shown["key"]) or []:
                title = str(item.get("title") or "")
                if needle in title.casefold() or needle in str(item.get("subtitle") or "").casefold():
                    view = self._with_programme_art(_programme(item, shown, now), notes)
                    view["channel_key"] = shown["key"]
                    view["channel_name"] = shown["name"]
                    programmes.append(view)
                    if len(programmes) >= 20:
                        break
        if indexed:
            try:
                hits = self.index.programmes(needle)
            except sqlite3.Error:
                hits = []
            for key, item in hits:
                shown = by_key.get(key)
                if shown is None:
                    continue
                view = self._with_programme_art(_programme(item, shown, now), notes)
                view["channel_key"] = shown["key"]
                view["channel_name"] = shown["name"]
                programmes.append(view)
                if len(programmes) >= 20:
                    break
        lap("guide")
        self._flush_notes(notes)
        lap("notes")
        shows = self.series(q=q, category=None, kids_only=kids_only, limit=20)["series"]
        lap("series")
        films = self.movies(q=q, category=None, kids_only=kids_only, limit=20)["movies"]
        lap("movies")
        from .iptvcanon import peek

        found = peek(self.db)
        lap("peek")
        _mark_canonical(self.db, shows, "series", found)
        _mark_canonical(self.db, films, "movie", found)
        lap("canonical")
        return {"q": q.strip(), "channels": channels[:20], "programmes": programmes[:20], "series": shows, "movies": films}


def _mark_canonical(db: Any, rows: list[dict[str, Any]], kind: str, found: Any = False) -> None:
    """Adds `canonical_id` (optional for the apps) from the folded catalog: the title this row's own id belongs
    to, else the same cleaned name. Never waits for a big catalog to be indexed; rows go without it meanwhile."""
    from .iptvcanon import parse, peek

    found = peek(db) if found is False else found  # a caller marking several lists peeks once
    if found is None:
        return
    want = "series" if kind == "series" else "movie"
    for row in rows:
        item = found.by_ref.get(str(row.get("id") or ""))
        if item is None or item.get("kind") != want:
            key = parse(str(row.get("name") or ""), None, titles=True).key
            item = next(iter(found.by_title(want, key)), None)
        if item is not None and item.get("id"):
            row["canonical_id"] = item["id"]


def _categories(rows: list[dict[str, Any]] | None) -> dict[str, str]:
    out = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("category_id") is not None:
            name = _clip(row.get("category_name"), 40)
            if name:
                out[str(row["category_id"])] = name
    return out


def _archive_days(row: dict[str, Any]) -> int:
    try:
        if int(row.get("tv_archive") or 0) != 1:
            return 0
        return max(0, min(14, int(row.get("tv_archive_duration") or 0)))
    except (TypeError, ValueError):
        return 0


def _num(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 99999 else None


def _duration(value: object, clock: object = None) -> int | None:
    """Seconds, from `duration_secs` or the panel's "01:41:00". Some panels send a minute count or a placeholder
    in `duration_secs` (films read "1 min"), so under two minutes the clock text wins, and without one it's unknown."""
    try:
        seconds = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = 0
    parts = str(clock or "").strip().split(":")
    if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
        from_clock = 0
        for p in parts if len(parts) == 3 else ["0", *parts]:
            from_clock = from_clock * 60 + int(p)
        if seconds < 120 and from_clock >= 120:
            seconds = from_clock
    if seconds < 120:
        return None
    return seconds if seconds <= 24 * 3600 else None


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _stamp_text(stamp: tuple[int, int]) -> str:
    return f"{stamp[0]}:{stamp[1]}"


def _year_of(value: object) -> int | None:
    text = str(value or "").strip()[:4]
    return int(text) if text.isdigit() else None


def _epoch(value: object) -> int | None:
    """When the provider added a title (unix seconds, sent as text by Xtream panels). Junk or 0 is None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 946684800 <= n <= 4102444800 else None


def _tmdb_id(value: object) -> str | None:
    """A provider's TMDB id, when it looks like one. Panels send 0, "" or junk for unknown."""
    text = str(value or "").strip()
    return text if text.isdigit() and int(text) > 0 and len(text) <= 10 else None


def _ext(value: object) -> str | None:
    """A provider file type (mp4, mkv, ...). The phone player only opens some of them."""
    text = str(value or "").strip().lower().lstrip(".")
    return text if text.isalnum() and len(text) <= 5 else None


def _title(row: dict[str, Any], key_of: Any, categories: dict[str, str], art_field: str,
           id_field: str = "stream_id") -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    key = key_of(row.get(id_field))
    if key is None:
        return None
    backdrop = row.get("backdrop_path")
    if isinstance(backdrop, list):
        backdrop = backdrop[0] if backdrop else None
    item = {
        "id": key, "name": _clip(row.get("name") or row.get("title"), 120) or "Untitled",
        "cover": _art(row.get(art_field)),
        "category": categories.get(str(row.get("category_id") or "")),
        "year": _clip(row.get("year") or row.get("releaseDate"), 20),
        "genre": _clip(row.get("genre"), 80), "rating": _clip(row.get("rating"), 8),
    }
    tmdb = _tmdb_id(row.get("tmdb") or row.get("tmdb_id"))
    if tmdb:
        item["tmdb"] = tmdb
    added = _epoch(row.get("added") or row.get("last_modified"))
    if added:
        item["added"] = added
    ext = _ext(row.get("container_extension"))
    if ext:
        item["ext"] = ext
    wide = _art(backdrop)
    if wide:
        item["backdrop"] = wide
    return item


def _parse_xmltv(xml: bytes | Path, epg_ids: dict[str, list[str]], now: float) -> dict[str, list[dict[str, Any]]]:
    """Programmes per channel key. Reads the document as a stream; several channels can share one guide id."""
    if not xml or not epg_ids:
        return {}
    folded = {name.casefold(): keys for name, keys in epg_ids.items()}
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        raw = xml.open("rb") if isinstance(xml, Path) else io.BytesIO(xml)
    except OSError:
        return {}
    with raw:
        head = raw.read(2)
        raw.seek(0)
        stream = gzip.GzipFile(fileobj=raw) if head == b"\x1f\x8b" else raw
        try:
            for _event, node in ET.iterparse(stream, events=("end",)):
                if _local(node.tag) != "programme":
                    continue
                _take_programme(node, epg_ids, folded, now, out)
                node.clear()
        except (ET.ParseError, OSError, EOFError) as exc:
            log.info("tv guide: stopped reading at %s", type(exc).__name__)
    for key in list(out):
        out[key] = _keep(out[key], now)
    return out


def _keep(bucket: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """What is on now and next first, then the most recent past (catch-up), MAX_PROGRAMMES in all."""
    bucket.sort(key=lambda item: item["start_ts"])
    ahead = [item for item in bucket if item["end_ts"] > now][:MAX_AHEAD]
    behind = [item for item in bucket if item["end_ts"] <= now][-(MAX_PROGRAMMES - len(ahead)):] if len(ahead) < MAX_PROGRAMMES else []
    return behind + ahead


def _take_programme(node: ET.Element, epg_ids: dict[str, list[str]], folded: dict[str, list[str]], now: float,
                    out: dict[str, list[dict[str, Any]]]) -> None:
    name = node.attrib.get("channel") or ""
    keys = epg_ids.get(name) or folded.get(name.casefold())
    if not keys:
        return
    begins = _xmltv_time(node.attrib.get("start") or "")
    finishes = _xmltv_time(node.attrib.get("stop") or "")
    if begins is None or finishes is None or finishes <= begins:
        return
    if finishes < now - GUIDE_BACK_S or begins > now + GUIDE_AHEAD_S:
        return
    icon = None
    for child in node:
        if _local(child.tag) == "icon":
            icon = _art(child.attrib.get("src"))
            break
    item = {
        "start_ts": begins, "end_ts": finishes, "title": _text(node, "title") or "Programme",
        "subtitle": _text(node, "sub-title"), "desc": _clip(_text(node, "desc"), 400), "icon": icon,
    }
    for key in keys:
        bucket = out.setdefault(key, [])
        bucket.append(dict(item))
        if len(bucket) >= 4 * MAX_PROGRAMMES:
            out[key] = _keep(bucket, now)


def _programme(item: dict[str, Any], channel: dict[str, Any], now: float) -> dict[str, Any]:
    begins, finishes = float(item["start_ts"]), float(item["end_ts"])
    days = int(channel.get("catchup_days") or 0)
    catchup = days > 0 and begins <= now and begins >= now - days * 86400
    return {
        "start": iso(begins), "end": iso(finishes), "title": item.get("title"),
        "subtitle": item.get("subtitle"), "desc": item.get("desc"), "icon": item.get("icon"),
        "catchup": catchup,
        "catchup_until": iso(begins + days * 86400) if catchup else None,
    }


def _readable(row: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Optional `title` (the name as people read it) and `category_label`, so screens show "Dune", not
    "EN - Dune (2021) 4K". `name` stays the provider's own, which the TVs key their lists by."""
    name, year = tvnames.polish(row.get("name"))
    if name and name != row.get("name"):
        out["title"] = name
    if year and not out.get("year"):
        out["year"] = year
    if row.get("category"):
        out["category_label"] = tvnames.category(row["category"])
    return out


def _public_series_row(row: dict[str, Any]) -> dict[str, Any]:
    return _readable(row, {
        "id": row.get("id"), "name": row.get("name"), "cover": row.get("cover"), "year": row.get("year"),
        "genre": row.get("genre"), "rating": row.get("rating"), "category": row.get("category"),
        "kids": bool(row.get("kids")), **({"added": row["added"]} if row.get("added") else {}),
    })


def _public_series(record: dict[str, Any]) -> dict[str, Any]:
    out = _public_series_row(record)
    out["plot"] = record.get("plot")
    out["backdrop"] = record.get("backdrop")
    out["seasons"] = record.get("seasons") or []
    return out


def _public_movie(row: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    out = _readable(row, {
        "id": row.get("id"), "name": row.get("name"), "cover": row.get("cover"), "year": row.get("year"),
        "genre": row.get("genre"), "rating": row.get("rating"), "category": row.get("category"),
        "kids": bool(row.get("kids")), **({"added": row["added"]} if row.get("added") else {}),
    })
    if detail or row.get("plot") or row.get("duration_s"):
        out["plot"] = row.get("plot")
        out["backdrop"] = row.get("backdrop")
        out["duration_s"] = row.get("duration_s")
    return out


def _with_canon(row: dict[str, Any], canon: dict[str, str]) -> None:
    """Add the tune id for an `xc:<provider>:<stream_id>` channel the catalog has folded."""
    parts = str(row.get("key") or "").split(":")
    if len(parts) != 3 or parts[0] != "xc" or not parts[2]:
        return
    found = canon.get(f"{parts[1]}|live|{parts[2]}")
    if isinstance(found, str) and found.startswith("can_"):
        row["canonical_id"] = found


EXTRA_WAIT_S = 180.0  # a provider's full film list can take minutes to come back


def _list_body(response: httpx.Response) -> list[Any]:
    """A provider list, or ValueError with a short reason the providers sheet can show."""
    if response.status_code >= 300:
        raise ValueError(f"answered {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise ValueError("answered something that isn't a list") from None
    if isinstance(body, dict) and "user_info" in body:
        raise ValueError("the provider refused the login")
    if not isinstance(body, list):
        raise ValueError("answered something that isn't a list")
    return body


def _read_reason(exc: Exception) -> str:
    """A few words for why one list read failed. Never an address, a login or a query string."""
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.HTTPError):
        return "couldn't connect"
    if isinstance(exc, TvAppError):
        text = exc.message.casefold()
        if "timed out" in text:
            return "timed out"
        if "redirect" in text:
            return "was redirected"
        return "couldn't connect"
    if isinstance(exc, ValueError) and str(exc) in ("the provider refused the login",
                                                     "answered something that isn't a list") \
            or str(exc).startswith("answered "):
        return str(exc)
    return "couldn't be read"
