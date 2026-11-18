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
import os
import logging
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .artwork import programme_key
from . import iptvaddr, tvnames
from .home import iso
from .tvapp import TvAppError
from .tvlineup import LineupError, clean_logo, public_channel

log = logging.getLogger("hub.tvcatalog")

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


class TvCatalog:
    def __init__(self, vault: Any, db: Any, data: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        # Set by the hub: asks the model about titles the rules were unsure of, after each refresh.
        self.settle: Any = None
        self.vault = vault
        self.db = db
        self.path = Path(data) / "tvapp" / "catalog.json"
        self.details_path = Path(data) / "tvapp" / "catalog-details.json"
        self._transport = transport
        self.kids_categories: KidsFn = lambda: []
        self._lock = asyncio.Lock()

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
        try:
            doc = json.loads(self.details_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"series": {}, "movies": {}, "episodes": {}}
        if not isinstance(doc, dict):
            return {"series": {}, "movies": {}, "episodes": {}}
        doc.setdefault("series", {})
        doc.setdefault("movies", {})
        doc.setdefault("episodes", {})
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc))
        self._loaded = None

    def _write_details(self, doc: dict[str, Any]) -> None:
        self.details_path.parent.mkdir(parents=True, exist_ok=True)
        self.details_path.write_text(json.dumps(doc))

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
        names = {str(c).strip().casefold() for c in self.kids_categories() if str(c).strip()}
        return category.strip().casefold() in names

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
        row = next((item for item in self.load().get(bucket) or [] if item.get("id") == ref), None)
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
                     now: float | None = None) -> dict[str, Any]:
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
        """Read every other provider's lists. One provider failing keeps its last list and doesn't stop the rest."""
        result: dict[str, str] = {}
        logins = self._extra_logins()
        for pid, lg in logins.items():
            params = {"username": lg["username"], "password": lg["password"]}
            try:
                got = []
                servers = list(lg["servers"])
                for action in ("get_live_streams", "get_vod_streams", "get_series"):
                    used, r = await self._api(pid, servers, {**params, "action": action}, wait_s=60)
                    if r.status_code >= 300:
                        raise TvAppError(502, "bad_provider", f"answered {r.status_code}")
                    # Stay on the address that just answered for the rest of this read.
                    servers = [used] + [s for s in servers if s != used]
                    part = r.json()
                    got.append(part if isinstance(part, list) else [])
                doc = self.ingest_extra(pid, live=got[0], vod=got[1], series=got[2])
                result[pid] = f"{len(doc['channels'])} channels, {len(doc['movies'])} films, {len(doc['series'])} series"
                log.info("provider %s: %s", pid, result[pid])
            except (httpx.HTTPError, TvAppError, ValueError) as exc:
                result[pid] = "not read"
                log.info("provider %s: lists not read (%s)", pid, type(exc).__name__)
        if logins:
            self.fold()
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
                    "duration_s": _duration(meta.get("duration_secs")),
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
            "duration_s": _duration(info.get("duration_secs")),
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
        try:
            await self.refresh()
        except TvAppError:
            if doc:
                return doc
            raise
        return self.load()

    async def refresh(self) -> dict[str, Any]:
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
                        return response.json()
                    except json.JSONDecodeError:
                        raise TvAppError(502, "bad_provider", "The TV provider sent something that isn't a list") from None

                if first.status_code >= 400:
                    raise TvAppError(502, "bad_provider", "The TV provider didn't answer")
                try:
                    live = first.json()
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
                doc = self.ingest(live=live, live_categories=live_categories, vod=vod, vod_categories=vod_categories,
                                  series=series, series_categories=series_categories, xmltv=xmltv)
            finally:
                if isinstance(xmltv, Path):
                    xmltv.unlink(missing_ok=True)
        await self.refresh_extras()
        if self.settle is not None:
            try:
                await self.settle()
            except Exception as exc:  # noqa: BLE001
                log.info("catalog match: %s", type(exc).__name__)
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

    def lineup(self, *, kids_only: bool) -> dict[str, Any] | None:
        doc = self.load()
        if not doc.get("channels"):
            return None
        rows = []
        seen: set[str] = set()
        overlay = self._overlay()
        canon = self._live_canon()
        for row in doc["channels"]:
            shown = self._present_channel(row, overlay)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            public = {key: shown[key] for key in ("key", "name", "number", "group", "logo", "kids")}
            if shown.get("catchup_days"):
                public["catchup_days"] = shown["catchup_days"]
            public["logo_url"] = self._art("logo", shown["key"])
            if shown.get("group"):
                # The provider's category made readable once here ("|US| SPORTS" as "Sports"); screens show it.
                public["group_label"] = tvnames.category(shown["group"])
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
            from .iptvcanon import KV
            state = self.db.kv_get(KV) or {}
        except Exception as exc:  # noqa: BLE001
            log.info("lineup canonical ids: %s", exc)
            return {}
        by_source = state.get("by_source") if isinstance(state, dict) else None
        return by_source if isinstance(by_source, dict) else {}

    def basic(self, home: tuple[float, float] | None) -> dict[str, Any] | None:
        """The household's basic cable lineup (see tvbasic). Worked out once per provider snapshot."""
        from . import tvbasic
        doc = self.load()
        if not doc.get("channels"):
            return None
        stamp = (doc.get("fetched_at"), doc.get("guide_at"), home, tvbasic.VERSION, len(self._overlay()))
        cached = getattr(self, "_basic_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        rows = []
        overlay = self._overlay()
        for row in doc["channels"]:
            shown = self._present_channel(row, overlay)
            if shown is not None:
                rows.append({"key": shown["key"], "name": shown["name"], "category": shown["group"]})
        guide = doc.get("guide") or {}
        out = tvbasic.build(rows, home, has_guide=lambda key: bool(guide.get(key)))
        self._basic_cache = (stamp, out)
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
            "number": number,
            "group": over.get("group") or row.get("category"),
            "logo": over.get("logo") or row.get("logo"),
            "kids": bool(over.get("kids")) or self._is_kids(row.get("category")),
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
        rows = []
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
            item = dict(row)
            item["kids"] = kids
            item.pop("category_id", None)
            rows.append(_public_movie(item) if kind == "movies" else _public_series_row(item))
        by_name = sorted(range(len(rows)), key=lambda i: str(rows[i].get("name") or "").casefold())
        if sort == "year":
            order = sorted(by_name, key=lambda i: -(_year_of(rows[i].get("year")) or 0))
        elif sort == "added":
            order = sorted(range(len(rows)), key=lambda i: (-(rows[i].get("added") or 0), -i))
        else:
            order = by_name
        label = "movies" if kind == "movies" else "series"
        start = max(0, offset)
        page = [rows[i] for i in order[start:start + limit]]
        # Art only for the rows sent back, noted in one write.
        notes: dict[str, dict[str, Any]] = {}
        shown = [self._with_title_art(row, show=kind != "movies", notes=notes) for row in page]
        self._flush_notes(notes)
        return {"categories": sorted(names), label: shown, "total": len(rows), "offset": start}

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

    def search(self, q: str, *, kids_only: bool, now: float | None = None) -> dict[str, Any]:
        needle = q.strip().casefold()
        now = time.time() if now is None else now
        channels = []
        programmes = []
        doc = self.load()
        overlay = self._overlay()
        notes: dict[str, dict[str, Any]] = {}
        for row in doc.get("channels") or []:
            shown = self._present_channel(row, overlay)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            if needle in str(shown["name"]).casefold():
                card = {key: shown[key] for key in ("key", "name", "number", "group", "logo", "kids")}
                card["logo_url"] = self._art("logo", shown["key"])
                channels.append(card)
            for item in (doc.get("guide") or {}).get(shown["key"]) or []:
                title = str(item.get("title") or "")
                if needle in title.casefold() or needle in str(item.get("subtitle") or "").casefold():
                    view = self._with_programme_art(_programme(item, shown, now), notes)
                    view["channel_key"] = shown["key"]
                    view["channel_name"] = shown["name"]
                    programmes.append(view)
                    if len(programmes) >= 20:
                        break
        self._flush_notes(notes)
        shows = self.series(q=q, category=None, kids_only=kids_only, limit=20)["series"]
        films = self.movies(q=q, category=None, kids_only=kids_only, limit=20)["movies"]
        _mark_canonical(self.db, shows, "series")
        _mark_canonical(self.db, films, "movie")
        return {"q": q.strip(), "channels": channels[:20], "programmes": programmes[:20], "series": shows, "movies": films}


def _mark_canonical(db: Any, rows: list[dict[str, Any]], kind: str) -> None:
    from .iptvcanon import playable

    for row in rows:
        found = playable(db, kind=kind, title=str(row.get("name") or ""), year=None)
        if found and found.get("canonical_id"):
            row["canonical_id"] = found["canonical_id"]


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


def _duration(value: object) -> int | None:
    try:
        seconds = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds <= 24 * 3600 else None


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


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


def _public_series_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"), "name": row.get("name"), "cover": row.get("cover"), "year": row.get("year"),
        "genre": row.get("genre"), "rating": row.get("rating"), "category": row.get("category"),
        "kids": bool(row.get("kids")), **({"added": row["added"]} if row.get("added") else {}),
    }


def _public_series(record: dict[str, Any]) -> dict[str, Any]:
    out = _public_series_row(record)
    out["plot"] = record.get("plot")
    out["backdrop"] = record.get("backdrop")
    out["seasons"] = record.get("seasons") or []
    return out


def _public_movie(row: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    out = {
        "id": row.get("id"), "name": row.get("name"), "cover": row.get("cover"), "year": row.get("year"),
        "genre": row.get("genre"), "rating": row.get("rating"), "category": row.get("category"),
        "kids": bool(row.get("kids")), **({"added": row["added"]} if row.get("added") else {}),
    }
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
