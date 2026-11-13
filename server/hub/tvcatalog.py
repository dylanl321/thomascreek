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
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .home import iso
from .tvapp import TvAppError
from .tvlineup import LineupError, clean_logo, public_channel

log = logging.getLogger("hub.tvcatalog")

SOURCE = "thomas-creek"
LIST_TTL_S = 6 * 3600
GUIDE_TTL_S = 3 * 3600
DETAIL_TTL_S = 24 * 3600
MAX_GUIDE_BYTES = 40 * 1024 * 1024
GUIDE_BACK_S = 7 * 86400
GUIDE_AHEAD_S = 36 * 3600
MAX_PROGRAMMES = 40
MAX_LIST = 100

KidsFn = Callable[[], list[str]]


def _art(url: object) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        return clean_logo(url.strip())
    except LineupError:
        return None


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


def episode_key(episode_id: object) -> str | None:
    part = _id_part(episode_id)
    return f"ep:{SOURCE}:{part}" if part else None


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
        self.vault = vault
        self.db = db
        self.path = Path(data) / "tvapp" / "catalog.json"
        self.details_path = Path(data) / "tvapp" / "catalog-details.json"
        self._transport = transport
        self.kids_categories: KidsFn = lambda: []
        self._lock = asyncio.Lock()

    # -- cache -----------------------------------------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return doc if isinstance(doc, dict) else {}

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

    def _is_kids(self, category: str | None) -> bool:
        if not category:
            return False
        names = {str(c).strip().casefold() for c in self.kids_categories() if str(c).strip()}
        return category.strip().casefold() in names

    def configured(self) -> bool:
        return all(self.vault.has(n) for n in ("iptv.xtream_server", "iptv.xtream_username", "iptv.xtream_password"))

    def _login(self) -> tuple[str, str, str]:
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

    # -- build -----------------------------------------------------------------------------------------------------
    def ingest(self, *, live: list[dict[str, Any]] | None = None, live_categories: list[dict[str, Any]] | None = None,
               vod: list[dict[str, Any]] | None = None, vod_categories: list[dict[str, Any]] | None = None,
               series: list[dict[str, Any]] | None = None, series_categories: list[dict[str, Any]] | None = None,
               xmltv: bytes | None = None, now: float | None = None) -> dict[str, Any]:
        """Store a provider snapshot. Tests pass the payloads in; refresh() downloads them."""
        now = time.time() if now is None else now
        live_names = _categories(live_categories)
        vod_names = _categories(vod_categories)
        series_names = _categories(series_categories)
        channels = []
        epg_ids: dict[str, str] = {}
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
                epg_ids[str(channels[-1]["epg_id"])] = key
        movies = [_title(row, movie_key, vod_names, "stream_icon") for row in vod or []]
        shows = [_title(row, series_key, series_names, "cover", id_field="series_id") for row in series or []]
        guide = _parse_xmltv(xmltv or b"", epg_ids, now) if xmltv else {}
        doc = {
            "fetched_at": now, "guide_at": now if xmltv is not None else 0,
            "channels": [c for c in channels if c], "movies": [m for m in movies if m],
            "series": [s for s in shows if s], "guide": guide,
            "categories": {
                "live": sorted({c["category"] for c in channels if c.get("category")}),
                "vod": sorted(set(vod_names.values())),
                "series": sorted(set(series_names.values())),
            },
        }
        self._write(doc)
        return doc

    def store_series(self, payload: dict[str, Any], series_id: str) -> dict[str, Any]:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        episodes_in = payload.get("episodes") if isinstance(payload.get("episodes"), dict) else {}
        doc = self.load()
        known = next((row for row in doc.get("series") or [] if row.get("id") == series_id), {})
        kids = self._is_kids(known.get("category"))
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
                eid = episode_key(ep.get("id"))
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
                    "series_name": known.get("name"), "fetched_at": time.time(),
                }
            shown.sort(key=lambda item: (item["number"] is None, item["number"] or 0))
            if shown:
                seasons.append({"number": season_no, "episodes": shown})
        seasons.sort(key=lambda season: season["number"])
        record = {
            "id": series_id, "name": known.get("name") or _clip(info.get("name"), 120),
            "cover": _art(info.get("cover")) or known.get("cover"),
            "backdrop": _art((info.get("backdrop_path") or [None])[0] if isinstance(info.get("backdrop_path"), list) else None),
            "plot": _clip(info.get("plot"), 600), "genre": _clip(info.get("genre"), 80),
            "year": _clip(info.get("releaseDate") or info.get("year"), 20),
            "category": known.get("category"), "kids": kids, "seasons": seasons, "fetched_at": time.time(),
        }
        details["series"][series_id] = record
        self._write_details(details)
        return _public_series(record)

    def store_movie(self, payload: dict[str, Any], movie_id: str) -> dict[str, Any]:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        doc = self.load()
        known = next((row for row in doc.get("movies") or [] if row.get("id") == movie_id), {})
        record = {
            "id": movie_id, "name": known.get("name") or _clip(info.get("name"), 120),
            "cover": _art(info.get("movie_image")) or known.get("cover"),
            "backdrop": _art((info.get("backdrop_path") or [None])[0] if isinstance(info.get("backdrop_path"), list) else None),
            "plot": _clip(info.get("plot"), 600), "genre": _clip(info.get("genre"), 80),
            "year": _clip(info.get("releasedate") or info.get("year"), 20),
            "duration_s": _duration(info.get("duration_secs")),
            "category": known.get("category"), "kids": self._is_kids(known.get("category")),
            "fetched_at": time.time(),
        }
        details = self._details()
        details["movies"][movie_id] = record
        self._write_details(details)
        return _public_movie(record, detail=True)

    # -- fetch -----------------------------------------------------------------------------------------------------
    async def ensure(self) -> dict[str, Any]:
        doc = self.load()
        now = time.time()
        lists_ok = bool(doc) and now - float(doc.get("fetched_at") or 0) < LIST_TTL_S
        guide_ok = now - float(doc.get("guide_at") or 0) < GUIDE_TTL_S
        if lists_ok and guide_ok:
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
            server, user, password = self._login()
            params = {"username": user, "password": password}
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

                live = await pull("get_live_streams")
                live_categories = await pull("get_live_categories")
                vod = await pull("get_vod_streams")
                vod_categories = await pull("get_vod_categories")
                series = await pull("get_series")
                series_categories = await pull("get_series_categories")
                xmltv = b""
                guide_url = str(self.vault.get("iptv.epg_url") or "").strip() or f"{server}/xmltv.php"
                try:
                    response = await client.get(guide_url, params=None if self.vault.get("iptv.epg_url") else params)
                    if response.status_code == 200 and len(response.content) <= MAX_GUIDE_BYTES:
                        xmltv = response.content
                except httpx.HTTPError as exc:
                    log.info("tv guide: %s", type(exc).__name__)
            if not all(isinstance(part, list) for part in (live, live_categories, vod, vod_categories, series, series_categories)):
                raise TvAppError(502, "bad_provider", "The TV provider didn't send its lists")
            return self.ingest(live=live, live_categories=live_categories, vod=vod, vod_categories=vod_categories,
                               series=series, series_categories=series_categories, xmltv=xmltv)

    async def fetch_series(self, series_id: str) -> dict[str, Any]:
        cached = self._details()["series"].get(series_id)
        if cached and time.time() - float(cached.get("fetched_at") or 0) < DETAIL_TTL_S:
            return _public_series(cached)
        server, user, password = self._login()
        part = series_id.split(":")[-1]
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, transport=self._transport) as client:
            response = await client.get(f"{server}/player_api.php", params={
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
            return _public_movie(cached, detail=True)
        server, user, password = self._login()
        part = movie_id.split(":")[-1]
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, transport=self._transport) as client:
            response = await client.get(f"{server}/player_api.php", params={
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
        for row in doc["channels"]:
            shown = self._present_channel(row)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            public = {key: shown[key] for key in ("key", "name", "number", "group", "logo", "kids")}
            if shown.get("catchup_days"):
                public["catchup_days"] = shown["catchup_days"]
            rows.append(public)
            seen.add(shown["key"])
        for key, over in self._overlay().items():
            if key in seen or over.get("hidden"):
                continue
            if kids_only and not over.get("kids"):
                continue
            rows.append(public_channel(over))
        rows.sort(key=lambda row: (row.get("number") is None, row.get("number") or 0, str(row.get("name") or "").casefold()))
        return {"version": int(doc.get("fetched_at") or 0), "channels": rows}

    def channel(self, key: str) -> dict[str, Any] | None:
        for row in self.load().get("channels") or []:
            if row.get("key") == key:
                return self._present_channel(row)
        return None

    def _present_channel(self, row: dict[str, Any]) -> dict[str, Any] | None:
        over = self._overlay().get(row["key"]) or {}
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

    def guide(self, *, start: float, end: float, channel: str | None, kids_only: bool, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        doc = self.load()
        wanted = []
        for row in doc.get("channels") or []:
            shown = self._present_channel(row)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            if channel and shown["key"] != channel:
                continue
            programmes = []
            current = None
            nxt = None
            for item in (doc.get("guide") or {}).get(shown["key"]) or []:
                begins, finishes = float(item["start_ts"]), float(item["end_ts"])
                view = _programme(item, shown, now)
                if begins <= now < finishes and current is None:
                    current = view
                elif begins >= now and nxt is None:
                    nxt = view
                if finishes > start and begins < end:
                    programmes.append(view)
            if channel or programmes or current:
                wanted.append({
                    "key": shown["key"], "name": shown["name"], "number": shown["number"],
                    "group": shown["group"], "logo": shown["logo"], "kids": shown["kids"],
                    "catchup_days": shown["catchup_days"] or None, "now": current, "next": nxt,
                    "programmes": programmes[:MAX_PROGRAMMES],
                })
        return {"from": iso(start), "to": iso(end), "channels": wanted[:200]}

    def movies(self, *, q: str | None, category: str | None, kids_only: bool, limit: int) -> dict[str, Any]:
        return self._list("movies", q=q, category=category, kids_only=kids_only, limit=limit)

    def series(self, *, q: str | None, category: str | None, kids_only: bool, limit: int) -> dict[str, Any]:
        return self._list("series", q=q, category=category, kids_only=kids_only, limit=limit)

    def _list(self, kind: str, *, q: str | None, category: str | None, kids_only: bool, limit: int) -> dict[str, Any]:
        needle = (q or "").strip().casefold()
        rows = []
        names: set[str] = set()
        for row in self.load().get(kind) or []:
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
        rows.sort(key=lambda item: str(item.get("name") or "").casefold())
        label = "movies" if kind == "movies" else "series"
        return {"categories": sorted(names), label: rows[:limit]}

    def movie(self, movie_id: str) -> dict[str, Any] | None:
        if not movie_id.startswith(f"vod:{SOURCE}:"):
            return None
        row = next((item for item in self.load().get("movies") or [] if item.get("id") == movie_id), None)
        if row is None:
            return None
        detail = self._details()["movies"].get(movie_id) or {}
        merged = {**row, **{k: v for k, v in detail.items() if v is not None}}
        merged["kids"] = self._is_kids(row.get("category"))
        return merged

    def episode(self, episode_id: str) -> dict[str, Any] | None:
        if not episode_id.startswith(f"ep:{SOURCE}:"):
            return None
        row = self._details()["episodes"].get(episode_id)
        if not row:
            return None
        series = next((item for item in self.load().get("series") or [] if item.get("id") == row.get("series_id")), {})
        kids = self._is_kids(series.get("category"))
        return {**row, "kids": kids, "series_name": series.get("name") or row.get("series_name")}

    def search(self, q: str, *, kids_only: bool, now: float | None = None) -> dict[str, Any]:
        needle = q.strip().casefold()
        now = time.time() if now is None else now
        channels = []
        programmes = []
        doc = self.load()
        for row in doc.get("channels") or []:
            shown = self._present_channel(row)
            if shown is None or (kids_only and not shown["kids"]):
                continue
            if needle in str(shown["name"]).casefold():
                channels.append({key: shown[key] for key in ("key", "name", "number", "group", "logo", "kids")})
            for item in (doc.get("guide") or {}).get(shown["key"]) or []:
                title = str(item.get("title") or "")
                if needle in title.casefold() or needle in str(item.get("subtitle") or "").casefold():
                    view = _programme(item, shown, now)
                    view["channel_key"] = shown["key"]
                    view["channel_name"] = shown["name"]
                    programmes.append(view)
                    if len(programmes) >= 20:
                        break
        shows = self.series(q=q, category=None, kids_only=kids_only, limit=20)["series"]
        films = self.movies(q=q, category=None, kids_only=kids_only, limit=20)["movies"]
        return {"q": q.strip(), "channels": channels[:20], "programmes": programmes[:20], "series": shows, "movies": films}


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


def _title(row: dict[str, Any], key_of: Any, categories: dict[str, str], art_field: str,
           id_field: str = "stream_id") -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    key = key_of(row.get(id_field))
    if key is None:
        return None
    return {
        "id": key, "name": _clip(row.get("name") or row.get("title"), 120) or "Untitled",
        "cover": _art(row.get(art_field)), "category": categories.get(str(row.get("category_id") or "")),
        "year": _clip(row.get("year") or row.get("releaseDate"), 20),
        "genre": _clip(row.get("genre"), 80), "rating": _clip(row.get("rating"), 8),
    }


def _parse_xmltv(xml: bytes, epg_ids: dict[str, str], now: float) -> dict[str, list[dict[str, Any]]]:
    if not xml or not epg_ids:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    for node in root:
        if _local(node.tag) != "programme":
            continue
        begins = _xmltv_time(node.attrib.get("start") or "")
        finishes = _xmltv_time(node.attrib.get("stop") or "")
        key = epg_ids.get(node.attrib.get("channel") or "")
        if begins is None or finishes is None or not key or finishes <= begins:
            continue
        if finishes < now - GUIDE_BACK_S or begins > now + GUIDE_AHEAD_S:
            continue
        icon = None
        for child in node:
            if _local(child.tag) == "icon":
                icon = _art(child.attrib.get("src"))
                break
        bucket = out.setdefault(key, [])
        if len(bucket) >= MAX_PROGRAMMES:
            continue
        bucket.append({
            "start_ts": begins, "end_ts": finishes, "title": _text(node, "title") or "Programme",
            "subtitle": _text(node, "sub-title"), "desc": _clip(_text(node, "desc"), 400), "icon": icon,
        })
    for bucket in out.values():
        bucket.sort(key=lambda item: item["start_ts"])
    return out


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
        "kids": bool(row.get("kids")),
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
        "kids": bool(row.get("kids")),
    }
    if detail or row.get("plot") or row.get("duration_s"):
        out["plot"] = row.get("plot")
        out["backdrop"] = row.get("backdrop")
        out["duration_s"] = row.get("duration_s")
    return out
