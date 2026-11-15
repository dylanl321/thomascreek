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

import hashlib
import json
import logging
import time
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
    def __init__(self, hub: Any):
        self.hub = hub
        self._at = 0.0
        self._doc: dict[str, Any] | None = None

    async def load(self, force: bool = False) -> dict[str, Any]:
        if self._doc is not None and not force and time.time() - self._at < TTL_S:
            return self._doc
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
        version = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]
        self._doc = {"version": version, **rows}
        self._at = time.time()
        return self._doc
