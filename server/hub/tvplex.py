"""Plex in the TV app's own library: every Plex film and show, with ids to fold them into the IPTV titles.

The TV lists Plex titles next to the provider's, folds a Plex film into the same tile as its IPTV copies
(TMDB id first, then title and year), and plays Plex through the hub: the file itself (direct play, Range
supported) behind a short-lived pass, so the Plex token never reaches the TV.

  GET  /v1/tv/plex/library              {"version","movies":[…],"shows":[…]}
  GET  /v1/tv/plex/{id}/episodes        {"episodes":[{"id","season","episode","title","duration_s","offset_s"}]}
  POST /v1/tv/plex/{id}/play            {"url":"/v1/tv/plex/file/<pass>","offset_s","resolution"}
  GET  /v1/tv/plex/file/{pass}          the file, Range supported
  POST /v1/tv/plex/{id}/progress        {offset_s, duration_s?, watched?} so Plex resumes there too
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

TTL_S = 15 * 60
PAGE = 500
MAX_ITEMS = 20000


def _year(value: object) -> int | None:
    try:
        year = int(str(value))
    except (TypeError, ValueError):
        return None
    return year if 1880 <= year <= 2100 else None


def _resolution(raw: dict[str, Any]) -> str | None:
    media = (raw.get("Media") or [{}])[0]
    value = str(media.get("videoResolution") or "").lower()
    return {"4k": "2160p", "1080": "1080p", "720": "720p", "480": "sd", "576": "sd", "sd": "sd"}.get(value)


def shape(raw: dict[str, Any], art: Any = None) -> dict[str, Any] | None:
    """One Plex library row as the TV gets it. Art goes through the hub's artwork cache."""
    from .adapters.media import _external_ids

    ident = str(raw.get("ratingKey") or "")
    title = str(raw.get("title") or "").strip()
    kind = raw.get("type")
    if not ident.isalnum() or not title or kind not in ("movie", "show"):
        return None
    tmdb, imdb, tvdb = _external_ids(raw)
    key = f"{'series' if kind == 'show' else 'vod'}:plex:{ident}"
    thumb = raw.get("thumb") if str(raw.get("thumb") or "").startswith("/library/") else None
    scene = raw.get("art") if str(raw.get("art") or "").startswith("/library/") else None
    out = {
        "id": ident, "kind": "series" if kind == "show" else "movie", "title": title, "year": _year(raw.get("year")),
        "tmdb": tmdb, "imdb": imdb, "tvdb": tvdb, "rating": raw.get("contentRating"),
        "summary": (str(raw.get("summary") or "")[:600] or None),
        "resolution": _resolution(raw) if kind == "movie" else None,
        "duration_s": int(raw["duration"]) // 1000 if kind == "movie" and raw.get("duration") else None,
        "offset_s": int(raw["viewOffset"]) // 1000 if raw.get("viewOffset") else None,
        "watched": bool(raw.get("viewCount")) if kind == "movie" else None,
        "added_at": int(raw["addedAt"]) if str(raw.get("addedAt") or "").isdigit() else None,
        "library": raw.get("librarySectionTitle"),
        "poster_url": art.public_url("poster", key) if art is not None and thumb else None,
        "backdrop_url": art.public_url("backdrop", key) if art is not None and (scene or thumb) else None,
        "_art": (key, thumb, scene or thumb),
    }
    return out


class PlexLibrary:
    """Every Plex film and show, kept for the house.

    A TV asks for it on each start, and rebuilding it from Plex took 2.6 s typically and 22 s at worst (Nov 21 2026
    review). So the last library is kept on disk and in memory: a caller gets it at once, and an old one is
    refreshed behind it, once, however many callers arrive. Only the very first load ever waits for Plex."""

    def __init__(self, hub: Any, path: Path | None = None):
        self.hub = hub
        self.path = path
        self._at = 0.0
        self._doc: dict[str, Any] | None = None
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._encoded: dict[Any, bytes] = {}

    async def load(self, force: bool = False) -> dict[str, Any]:
        if self._doc is None and not force and self.path is not None:
            await asyncio.to_thread(self._read_saved)
        if self._doc is not None and not force:
            if time.time() - self._at >= TTL_S:
                self._refresh_later()
            return self._doc
        return await self._fetch_once()

    def encoded(self, key: Any, build: Any) -> bytes:
        """The packed reply for one audience of the current library (`key` includes the version)."""
        memo = self._encoded.get(key)
        if memo is None:
            if len(self._encoded) > 16:
                self._encoded.clear()
            memo = self._encoded[key] = json.dumps(build(), separators=(",", ":")).encode()
        return memo

    def _read_saved(self) -> None:
        if self._doc is not None or self.path is None:
            return
        try:
            doc = json.loads(self.path.read_text())
            at = self.path.stat().st_mtime
        except (OSError, ValueError):
            return
        if isinstance(doc, dict) and isinstance(doc.get("movies"), list) and isinstance(doc.get("shows"), list):
            self._doc, self._at = doc, at

    def _save(self, doc: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.path)
        except OSError as exc:
            log.info("plex library: not saved (%s)", type(exc).__name__)

    async def _fetch_once(self) -> dict[str, Any]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._fetch())
            self._task.add_done_callback(_quiet)
        return await asyncio.shield(self._task)

    def _refresh_later(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._fetch())
        self._task.add_done_callback(_quiet)

    async def _fetch(self) -> dict[str, Any]:
        plex = self.hub.plex
        rows: dict[str, list[dict[str, Any]]] = {"movies": [], "shows": []}
        sections = (await plex.get_json("/library/sections")).get("MediaContainer", {}).get("Directory", [])
        art = getattr(self.hub, "artwork", None)
        notes: dict[str, dict[str, Any]] = {}
        for sec in sections:
            kind = sec.get("type")
            if kind not in ("movie", "show"):
                continue
            start = 0
            while start < MAX_ITEMS:
                data = await plex.get_json(f"/library/sections/{quote(str(sec['key']))}/all", params={
                    "X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE, "includeGuids": 1})
                page = data.get("MediaContainer", {}).get("Metadata", []) or []
                for raw in page:
                    raw.setdefault("librarySectionTitle", sec.get("title"))
                    item = shape(raw, art)
                    if item is None:
                        continue
                    key, thumb, scene = item.pop("_art")
                    if thumb or scene:
                        notes[key] = {"title": item["title"], "year": item["year"],
                                      "kind": "tv" if item["kind"] == "series" else "movie",
                                      "plex_thumb": thumb, "plex_art": scene}
                    rows["movies" if kind == "movie" else "shows"].append(item)
                if len(page) < PAGE:
                    break
                start += PAGE
        if art is not None and notes:
            remember_many = getattr(art, "remember_many", None)
            if remember_many is not None:
                remember_many(notes)
        doc = await asyncio.to_thread(_versioned, rows)
        if self._doc is None or doc["version"] != self._doc.get("version"):
            self._encoded.clear()
            self._doc = doc
            await asyncio.to_thread(self._save, doc)
        else:
            await asyncio.to_thread(_touch, self.path)
        self._at = time.time()
        return self._doc


def _versioned(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    version = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return {"version": version, **rows}


def _touch(path: Path | None) -> None:
    if path is not None:
        try:
            path.touch()
        except OSError:
            pass


def _quiet(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.info("plex library: refresh failed (%s)", type(task.exception()).__name__)
