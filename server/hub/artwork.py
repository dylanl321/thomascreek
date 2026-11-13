"""Artwork for the TV app: channel logos and title art, cached on the hub.

Channel logos come from the iptv-org database (matched by iptv-org id or tvg-id),
then a file in the tv-logo/tv-logos repository, then the provider's own logo.
Posters and backdrops come from TMDB, with fanart.tv clear art when that key is
set. Episode stills come from TVmaze. A missing key or a failed download leaves
that image out; the guide itself still loads.

Files are stored by the sha256 of their bytes. Indexes of the public logo
databases refresh weekly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("hub.artwork")

IPTV_CHANNELS = "https://iptv-org.github.io/api/channels.json"
IPTV_LOGOS = "https://iptv-org.github.io/api/logos.json"
TVLOGOS_TREE = "https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1"
TVLOGOS_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
TMDB = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p"
FANART = "https://webservice.fanart.tv/v3"
TVMAZE = "https://api.tvmaze.com"

INDEX_TTL_S = 7 * 86400
MISS_TTL_S = 24 * 3600
MAX_BYTES = 5 * 1024 * 1024
USER_AGENT = "thomascreek-hub"

KINDS = ("logo", "poster", "backdrop", "still")
SIZES = {
    "s": "w185", "m": "w342", "l": "w780", "original": "original",
    "w92": "w92", "w154": "w154", "w185": "w185", "w342": "w342", "w500": "w500", "w780": "w780",
}
_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif",
}
_EXT_TYPE = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_KEY = re.compile(
    r"^(?:iptv-org:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|xc:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|tvg:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|vod:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|series:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|ep:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}"
    r"|prog:[a-f0-9]{20})$"
)
_YEAR = re.compile(r"(?:19|20)\d{2}")


class ArtError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def programme_key(title: str, year: int | None = None) -> str:
    """Stable art id for a guide title. The same show on two channels shares one poster."""
    text = re.sub(r"[^a-z0-9]+", " ", (title or "").casefold()).strip()
    raw = f"{text}|{year or ''}"
    return "prog:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def normalize_size(size: str | None) -> str:
    return SIZES.get((size or "m").strip().lower(), "w342")


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")


def year_of(value: object) -> int | None:
    match = _YEAR.search(str(value or ""))
    return int(match.group(0)) if match else None


def pick_iptv_logo(rows: list[dict[str, Any]]) -> str | None:
    """Prefer a logo the broadcaster still uses, in a bitmap format, closest to a few hundred pixels."""
    ranked: list[tuple[int, int, str]] = []
    for row in rows:
        url = row.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        fmt = str(row.get("format") or "").upper()
        score = 3 if fmt in ("PNG", "WEBP", "JPEG", "JPG") else 1 if fmt == "SVG" else 2
        if row.get("in_use"):
            score += 2
        try:
            width = int(row.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        # A logo near 400px is the one a TV row wants. Huge billboards sort later.
        distance = abs(width - 400) if width else 1000
        ranked.append((score, -distance, url))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def pick_tvlogo(index: dict[str, list[str]], stems: list[str], country: str | None = None) -> str | None:
    """Match a tv-logos filename. `CNN.us` becomes `cnn-us`."""
    cc = (country or "").lower()
    for stem in stems:
        urls = index.get(stem) or []
        if not urls:
            continue
        if cc:
            for url in urls:
                if url.lower().endswith(f"-{cc}.png") or f"/{cc}/" in url.lower():
                    return url
        return urls[0]
    return None


def pick_tmdb(results: list[dict[str, Any]], year: int | None, date_field: str) -> dict[str, Any] | None:
    """The result whose release year matches, otherwise the first hit."""
    usable = [row for row in results if isinstance(row, dict) and row.get("id")]
    if year is not None:
        for row in usable:
            if str(row.get(date_field) or "").startswith(str(year)):
                return row
    return usable[0] if usable else None


def channel_lookup_ids(key: str, channel: dict[str, Any] | None) -> list[str]:
    """iptv-org id, then the provider tvg-id, without duplicates."""
    found: list[str] = []
    if key.startswith("iptv-org:"):
        found.append(key.split(":", 1)[1])
    epg = str((channel or {}).get("epg_id") or "").strip()
    if epg and epg not in found:
        found.append(epg)
    if key.startswith("tvg:"):
        tvg = key.split(":", 2)[-1]
        if tvg and tvg not in found:
            found.append(tvg)
    return found


class Artwork:
    def __init__(self, vault: Any, directory: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 tmdb_secret: str = "tmdb.api_key", fanart_secret: str = "fanart.api_key"):
        self.vault = vault
        self.tmdb_secret = tmdb_secret
        self.fanart_secret = fanart_secret
        self.dir = Path(directory)
        self.files = self.dir / "files"
        self._transport = transport
        self.catalog: Any = None
        self._task: asyncio.Task[None] | None = None
        self._indexes: dict[str, Any] = {"at": 0, "channels": {}, "logos": {}, "tvlogos": {}}
        self._notes: dict[str, dict[str, Any]] = {}
        self._cache: dict[str, dict[str, Any]] = {}
        self._load()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.prefetch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("artwork: %s", exc)
            await asyncio.sleep(6 * 3600)

    # -- public urls -----------------------------------------------------------------------------------------------
    def public_url(self, kind: str, key: str) -> str | None:
        if kind not in KINDS or not _KEY.fullmatch(key or ""):
            return None
        return f"/v1/art/{kind}/{key}"

    def remember(self, key: str, **meta: Any) -> None:
        clean = {k: v for k, v in meta.items() if v is not None}
        if self._notes.get(key) == clean:
            return
        self._notes[key] = clean
        self._save_json("notes.json", self._notes)

    def note(self, key: str) -> dict[str, Any]:
        return dict(self._notes.get(key) or {})

    # -- serve -----------------------------------------------------------------------------------------------------
    async def file(self, kind: str, key: str, size: str | None) -> tuple[Path, str, str]:
        if kind not in KINDS or not _KEY.fullmatch(key or ""):
            raise ArtError(404, "not_found", "No artwork")
        variant = normalize_size(size)
        slot = f"{kind}|{key}|{variant}"
        hit = self._cache.get(slot)
        now = time.time()
        if hit and hit.get("sha") and not self._expired(hit):
            path = self.files / f"{hit['sha']}.{hit['ext']}"
            if path.is_file():
                return path, f"\"{hit['sha']}\"", _EXT_TYPE.get(hit["ext"], "application/octet-stream")
        if hit and hit.get("miss") and now - float(hit.get("at") or 0) < MISS_TTL_S:
            raise ArtError(404, "not_found", "No artwork")
        url = await self._source_url(kind, key, variant)
        if not url:
            # A missing TMDB key is not a real miss: adding the key later should work the same day.
            needs_tmdb = kind in ("poster", "backdrop") or (
                kind == "logo" and key.startswith(("vod:", "series:", "prog:")))
            if not (needs_tmdb and not self._secret(self.tmdb_secret)):
                self._remember_cache(slot, {"miss": True, "at": now})
            raise ArtError(404, "not_found", "No artwork")
        downloaded = await self._download(url)
        if downloaded is None:
            self._remember_cache(slot, {"miss": True, "at": now})
            raise ArtError(404, "not_found", "No artwork")
        data, ext = downloaded
        digest = hashlib.sha256(data).hexdigest()
        self.files.mkdir(parents=True, exist_ok=True)
        path = self.files / f"{digest}.{ext}"
        if not path.is_file():
            path.write_bytes(data)
        self._remember_cache(slot, {"sha": digest, "ext": ext, "at": now, "url": _safe_url(url)})
        return path, f"\"{digest}\"", _EXT_TYPE[ext]

    # -- prefetch --------------------------------------------------------------------------------------------------
    async def prefetch(self) -> dict[str, int]:
        """Logos for the lineup, and posters for programmes that start in the next day."""
        await self.refresh_indexes()
        catalog = self.catalog
        if catalog is None:
            return {"logos": 0, "posters": 0}
        doc = catalog.load()
        logos = 0
        seen: set[str] = set()
        for row in doc.get("channels") or []:
            key = row.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            if await self._try("logo", key):
                logos += 1
        overlay = getattr(catalog, "_overlay", None)
        if overlay:
            for key in overlay():
                if key in seen:
                    continue
                seen.add(key)
                if await self._try("logo", key):
                    logos += 1
        posters = 0
        now = time.time()
        for items in (doc.get("guide") or {}).values():
            for item in items or []:
                start = float(item.get("start_ts") or 0)
                end = float(item.get("end_ts") or 0)
                if not (now - 3600 <= start <= now + 86400 or start <= now < end):
                    continue
                title = str(item.get("title") or "")
                if not title:
                    continue
                art_key = programme_key(title)
                self.remember(art_key, title=title, kind="tv")
                if await self._try("poster", art_key):
                    posters += 1
                await self._try("backdrop", art_key)
        return {"logos": logos, "posters": posters}

    async def _try(self, kind: str, key: str) -> bool:
        try:
            await self.file(kind, key, "m" if kind != "backdrop" else "l")
        except ArtError:
            return False
        return True

    # -- indexes ---------------------------------------------------------------------------------------------------
    async def refresh_indexes(self, *, force: bool = False) -> None:
        if not force and time.time() - float(self._indexes.get("at") or 0) < INDEX_TTL_S and self._indexes.get("logos"):
            return
        try:
            channels = await self._get_json(IPTV_CHANNELS)
            logos = await self._get_json(IPTV_LOGOS)
            tree = await self._get_json(TVLOGOS_TREE)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("artwork indexes: %s", type(exc).__name__)
            return
        if not isinstance(channels, list) or not isinstance(logos, list):
            return
        self._indexes = {
            "at": time.time(),
            "channels": _channel_index(channels),
            "logos": _logo_index(logos),
            "tvlogos": _tvlogo_index(tree if isinstance(tree, dict) else {}),
        }
        self._save_json("indexes.json", self._indexes)

    def use_indexes(self, *, channels: list[dict[str, Any]], logos: list[dict[str, Any]],
                    tvlogos: dict[str, list[str]] | None = None) -> None:
        """Install logo databases the caller already has. The weekly refresh does this from the network."""
        self._indexes = {
            "at": time.time(),
            "channels": _channel_index(channels),
            "logos": _logo_index(logos),
            "tvlogos": tvlogos or {},
        }

    # -- sources ---------------------------------------------------------------------------------------------------
    async def _source_url(self, kind: str, key: str, size: str) -> str | None:
        if kind == "logo" and not key.startswith(("vod:", "series:", "ep:", "prog:")):
            return self._channel_logo_url(key)
        if kind == "still":
            return await self._still_url(key, size)
        return await self._title_url(kind, key, size)

    def _channel(self, key: str) -> dict[str, Any] | None:
        catalog = self.catalog
        if catalog is None:
            return None
        for row in catalog.load().get("channels") or []:
            if row.get("key") == key:
                return row
        overlay = catalog._overlay().get(key) if hasattr(catalog, "_overlay") else None
        return overlay if isinstance(overlay, dict) else None

    def _channel_logo_url(self, key: str) -> str | None:
        channel = self._channel(key) or {}
        channels = self._indexes.get("channels") or {}
        logos = self._indexes.get("logos") or {}
        ids = channel_lookup_ids(key, channel)
        for iptv_id in ids:
            url = logos.get(iptv_id) or logos.get(iptv_id.lower())
            if url:
                return url
        info = {}
        for iptv_id in ids:
            info = channels.get(iptv_id) or channels.get(iptv_id.lower()) or info
        name = info.get("name") or channel.get("name")
        country = info.get("country") if isinstance(info.get("country"), str) else None
        stems = [slug(iptv_id) for iptv_id in ids]
        name_slug = slug(name)
        if name_slug:
            stems.append(name_slug)
            if country:
                stems.append(f"{name_slug}-{country.lower()}")
        stems = [stem for stem in stems if stem]
        url = pick_tvlogo(self._indexes.get("tvlogos") or {}, stems, country)
        if url:
            return url
        provider = channel.get("logo")
        if isinstance(provider, str) and provider.startswith("http"):
            return provider
        return None

    async def _title_url(self, kind: str, key: str, size: str) -> str | None:
        meta = self.note(key)
        if not meta and self.catalog is not None:
            meta = _meta_from_catalog(self.catalog, key)
            if meta:
                self.remember(key, **meta)
        title = meta.get("title") or meta.get("name")
        if not title and not meta.get("imdb") and not meta.get("tmdb"):
            return None
        found = await self._tmdb(meta, title, year_of(meta.get("year")), meta.get("kind") or "tv")
        if not found:
            return None
        fanart = await self._fanart(found)
        if kind == "logo":
            return (fanart or {}).get("logo")
        if kind == "backdrop":
            return (fanart or {}).get("backdrop") or _tmdb_image(found.get("backdrop_path"), "w780" if size == "w342" else size)
        return (fanart or {}).get("poster") or _tmdb_image(found.get("poster_path"), size)

    async def _still_url(self, key: str, size: str) -> str | None:
        meta = self.note(key)
        if not meta and self.catalog is not None:
            meta = _meta_from_catalog(self.catalog, key)
            if meta:
                self.remember(key, **meta)
        title = meta.get("title") or meta.get("series")
        season, number = meta.get("season"), meta.get("number")
        if not title or season is None or number is None:
            return None
        try:
            found = await self._get_json(f"{TVMAZE}/search/shows", {"q": title})
        except (httpx.HTTPError, ValueError):
            return None
        show_id = None
        if isinstance(found, list):
            for row in found:
                show = row.get("show") if isinstance(row, dict) else None
                if isinstance(show, dict) and show.get("id"):
                    show_id = show["id"]
                    break
        if show_id is None:
            return None
        try:
            episode = await self._get_json(f"{TVMAZE}/shows/{show_id}/episodebynumber", {"season": season, "number": number})
        except (httpx.HTTPError, ValueError):
            return None
        image = episode.get("image") if isinstance(episode, dict) else None
        if not isinstance(image, dict):
            return None
        field = "original" if size in ("w780", "original") else "medium"
        url = image.get(field) or image.get("original") or image.get("medium")
        return url if isinstance(url, str) else None

    async def _tmdb(self, meta: dict[str, Any], title: str | None, year: int | None, kind: str) -> dict[str, Any] | None:
        key = self._secret(self.tmdb_secret)
        if not key:
            return None
        params = {"api_key": key}
        try:
            imdb = meta.get("imdb")
            tmdb_id = meta.get("tmdb")
            if imdb:
                found = await self._get_json(f"{TMDB}/find/{imdb}", {**params, "external_source": "imdb_id"})
                bucket = "tv_results" if kind == "tv" else "movie_results"
                rows = found.get(bucket) if isinstance(found, dict) else None
                if not rows and isinstance(found, dict):
                    rows = found.get("movie_results") or found.get("tv_results")
                picked = pick_tmdb(rows or [], year, "first_air_date" if kind == "tv" else "release_date")
            elif tmdb_id:
                path = "tv" if kind == "tv" else "movie"
                one = await self._get_json(f"{TMDB}/{path}/{tmdb_id}", params)
                picked = one if isinstance(one, dict) and one.get("id") else None
            elif title:
                path = "search/tv" if kind == "tv" else "search/movie"
                query = {"query": title, **params}
                if year and kind == "tv":
                    query["first_air_date_year"] = year
                elif year:
                    query["year"] = year
                found = await self._get_json(f"{TMDB}/{path}", query)
                rows = found.get("results") if isinstance(found, dict) else None
                picked = pick_tmdb(rows or [], year, "first_air_date" if kind == "tv" else "release_date")
            else:
                picked = None
        except (httpx.HTTPError, ValueError):
            return None
        if not picked:
            return None
        picked = dict(picked)
        picked["_kind"] = "tv" if kind == "tv" else "movie"
        return picked

    async def _fanart(self, tmdb_row: dict[str, Any]) -> dict[str, str] | None:
        key = self._secret(self.fanart_secret)
        if not key:
            return None
        kind = tmdb_row.get("_kind") or "movie"
        art_id = tmdb_row.get("id")
        if kind == "tv":
            tmdb_key = self._secret(self.tmdb_secret)
            if not tmdb_key:
                return None
            try:
                extra = await self._get_json(f"{TMDB}/tv/{art_id}/external_ids", {"api_key": tmdb_key})
            except (httpx.HTTPError, ValueError):
                return None
            art_id = extra.get("tvdb_id") if isinstance(extra, dict) else None
            path = f"{FANART}/tv/{art_id}"
            logo_field, poster_field, backdrop_field = "hdtvlogo", "tvposter", "showbackground"
        else:
            path = f"{FANART}/movies/{art_id}"
            logo_field, poster_field, backdrop_field = "hdmovielogo", "movieposter", "moviebackground"
        if not art_id:
            return None
        try:
            payload = await self._get_json(path, {"api_key": key})
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "logo": _fanart_url(payload.get(logo_field) or payload.get("clearlogo")),
            "poster": _fanart_url(payload.get(poster_field)),
            "backdrop": _fanart_url(payload.get(backdrop_field)),
        }

    # -- http ------------------------------------------------------------------------------------------------------
    def _secret(self, name: str) -> str | None:
        if not self.vault.has(name):
            return None
        value = self.vault.get(name)
        text = str(value).strip() if value else ""
        return text or None

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=self._transport,
                                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            response = await client.get(url, params=params)
        if response.status_code >= 400:
            raise ValueError(str(response.status_code))
        return response.json()

    async def _download(self, url: str) -> tuple[bytes, str] | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, transport=self._transport,
                                          headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.content or len(response.content) > MAX_BYTES:
            return None
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = _TYPES.get(ctype)
        if ext is None:
            path = urlsplit(str(response.url)).path.lower()
            for suffix, name in (("png", "png"), ("jpg", "jpg"), ("jpeg", "jpg"), ("webp", "webp"), ("gif", "gif")):
                if path.endswith("." + suffix):
                    ext = name
                    break
        if ext is None or ext not in _EXT_TYPE:
            return None
        return response.content, ext

    # -- disk ------------------------------------------------------------------------------------------------------
    def _load(self) -> None:
        self._indexes = self._read_json("indexes.json") or self._indexes
        self._notes = self._read_json("notes.json") or {}
        self._cache = self._read_json("cache.json") or {}

    def _read_json(self, name: str) -> Any:
        try:
            doc = json.loads((self.dir / name).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return doc

    def _save_json(self, name: str, doc: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(json.dumps(doc))

    def _remember_cache(self, slot: str, row: dict[str, Any]) -> None:
        self._cache[slot] = row
        self._save_json("cache.json", self._cache)

    @staticmethod
    def _expired(hit: dict[str, Any]) -> bool:
        return time.time() - float(hit.get("at") or 0) > INDEX_TTL_S


def _safe_url(url: str) -> str:
    """Drop query strings so an API key in a URL is never written next to the file."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()


def _tmdb_image(path: object, size: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return f"{TMDB_IMAGE}/{size}{path}"


def _fanart_url(rows: object) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"].startswith("http"):
            return row["url"]
    return None


def _channel_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    out = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        out[str(row["id"])] = {"name": str(row.get("name") or ""), "country": str(row.get("country") or "")}
    return out


def _logo_index(rows: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("channel"):
            grouped.setdefault(str(row["channel"]), []).append(row)
    return {channel: url for channel, items in grouped.items() if (url := pick_iptv_logo(items))}


def _tvlogo_index(tree: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in tree.get("tree") or []:
        if not isinstance(row, dict) or row.get("type") != "blob":
            continue
        path = str(row.get("path") or "")
        if not path.lower().endswith(".png") or path.startswith("."):
            continue
        stem = Path(path).stem.lower()
        out.setdefault(stem, []).append(TVLOGOS_RAW + path)
    return out


def _meta_from_catalog(catalog: Any, key: str) -> dict[str, Any]:
    doc = catalog.load()
    if key.startswith("vod:"):
        row = next((item for item in doc.get("movies") or [] if item.get("id") == key), None)
        if not row:
            return {}
        return {"title": row.get("name"), "year": year_of(row.get("year")), "kind": "movie"}
    if key.startswith("series:"):
        row = next((item for item in doc.get("series") or [] if item.get("id") == key), None)
        if not row:
            return {}
        return {"title": row.get("name"), "year": year_of(row.get("year")), "kind": "tv"}
    if key.startswith("ep:"):
        details = catalog._details() if hasattr(catalog, "_details") else {}
        episode = (details.get("episodes") or {}).get(key) or {}
        if not episode:
            return {}
        return {
            "title": episode.get("series_name"), "series": episode.get("series_name"),
            "season": episode.get("season"), "number": episode.get("number"), "kind": "episode",
        }
    return {}
