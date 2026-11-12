"""Plex (browse, continue watching, artwork) and Jellyseerr (requests)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import Adapter


class Plex(Adapter):
    name = "plex"
    ping_path = "/library/sections"
    title = "Plex"

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        h = {"Accept": "application/json", "X-Plex-Product": "Thomas Creek Hub", "X-Plex-Client-Identifier": "thomascreek-hub"}
        if token:
            h["X-Plex-Token"] = token
        return h

    @staticmethod
    def _item(m: dict[str, Any]) -> dict[str, Any]:
        kind = m.get("type", "movie")
        title = m.get("title", "")
        if kind == "episode":
            title = f"{m.get('grandparentTitle', '')}: {title}".strip(": ")
        return {
            "id": str(m.get("ratingKey")),
            "title": title,
            "year": m.get("year"),
            "kind": kind,
            "poster_url": f"/v1/media/art?path={quote(m.get('grandparentThumb') or m.get('thumb') or '', safe='')}" if (m.get("thumb") or m.get("grandparentThumb")) else None,
            "rating": m.get("contentRating"),
            "summary": m.get("summary"),
            "library": m.get("librarySectionTitle"),
            "show": m.get("grandparentTitle"),
            "season": m.get("parentIndex"),
            "episode": m.get("index"),
            "view_offset_s": int(m.get("viewOffset", 0)) // 1000 if m.get("viewOffset") else None,
        }

    async def continue_watching(self) -> list[dict[str, Any]]:
        data = await self.get_json("/library/onDeck")
        return [self._item(m) for m in data.get("MediaContainer", {}).get("Metadata", [])]

    async def search(self, q: str, limit: int = 30) -> list[dict[str, Any]]:
        data = await self.get_json("/hubs/search", params={"query": q, "limit": limit})
        out: list[dict[str, Any]] = []
        for hub in data.get("MediaContainer", {}).get("Hub", []):
            if hub.get("type") not in ("movie", "show", "episode"):
                continue
            out.extend(self._item(m) for m in hub.get("Metadata", []))
        return out

    async def library(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sections = (await self.get_json("/library/sections")).get("MediaContainer", {}).get("Directory", [])
        out: list[dict[str, Any]] = []
        for sec in sections:
            if sec.get("type") not in ("movie", "show") or (kind and sec.get("type") != kind):
                continue
            data = await self.get_json(f"/library/sections/{quote(str(sec['key']))}/all",
                                       params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit,
                                               "sort": "titleSort"})
            for m in data.get("MediaContainer", {}).get("Metadata", []):
                m.setdefault("librarySectionTitle", sec.get("title"))
                out.append(self._item(m))
        return out[:limit]

    async def metadata(self, item_id: str) -> dict[str, Any] | None:
        data = await self.get_json(f"/library/metadata/{quote(item_id)}")
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return self._item(items[0]) if items else None

    async def art(self, path: str, width: int = 400) -> tuple[bytes, str]:
        if not path.startswith("/library/"):
            raise ValueError("bad art path")
        resp = await self.request(
            "GET", "/photo/:/transcode",
            params={"url": path, "width": width, "height": int(width * 1.5), "minSize": 1},
        )
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    async def sessions(self) -> list[dict[str, Any]]:
        data = await self.get_json("/status/sessions")
        return data.get("MediaContainer", {}).get("Metadata", [])

    @staticmethod
    def ha_play_payload(item: dict[str, Any]) -> tuple[str, str]:
        """media_content_type and media_content_id for HA's Plex play_media."""
        import json as _json

        if item["kind"] == "episode":
            return "episode", _json.dumps({
                "library_name": item.get("library"), "show_name": item.get("show"),
                "season_number": item.get("season"), "episode_number": item.get("episode"),
                "resume": True,
            })
        if item["kind"] == "show":
            return "episode", _json.dumps({"library_name": item.get("library"), "show_name": item["title"], "unwatched": True, "maxresults": 1})
        return "movie", _json.dumps({"library_name": item.get("library"), "title": item["title"], "resume": True})


class Jellyseerr(Adapter):
    name = "jellyseerr"
    title = "Requests"
    ping_path = "/api/v1/request?take=1"

    def auth_headers(self) -> dict[str, str]:
        key = self.secret(self.cfg.token_secret)
        return {"X-Api-Key": key} if key else {}

    async def search(self, q: str) -> list[dict[str, Any]]:
        data = await self.get_json("/api/v1/search", params={"query": q, "page": 1})
        out = []
        for r in data.get("results", []):
            if r.get("mediaType") not in ("movie", "tv"):
                continue
            out.append({
                "id": f"{r['mediaType']}:{r['id']}",
                "title": r.get("title") or r.get("name"),
                "year": (r.get("releaseDate") or r.get("firstAirDate") or "")[:4] or None,
                "kind": r["mediaType"],
                "poster_url": f"https://image.tmdb.org/t/p/w342{r['posterPath']}" if r.get("posterPath") else None,
                "summary": r.get("overview"),
            })
        return out

    async def create_request(self, media_id: str) -> dict[str, Any]:
        kind, tmdb = media_id.split(":", 1)
        body: dict[str, Any] = {"mediaType": kind, "mediaId": int(tmdb)}
        if kind == "tv":
            body["seasons"] = "all"
        return await self.post_json("/api/v1/request", body)

    async def request_status(self, request_id: str) -> dict[str, Any]:
        return await self.get_json(f"/api/v1/request/{quote(request_id)}")

    REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
    MEDIA_STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partly available", 5: "available", 6: "deleted"}

    async def recent_requests(self, take: int = 15) -> list[dict[str, Any]]:
        """Newest requests with their titles (Jellyseerr's list leaves titles out)."""
        import asyncio

        data = await self.get_json("/api/v1/request", params={"take": take, "skip": 0, "sort": "added", "filter": "all"})
        rows = data.get("results", [])

        async def title(r: dict[str, Any]) -> dict[str, Any]:
            media = r.get("media") or {}
            kind, tmdb = media.get("mediaType") or r.get("type"), media.get("tmdbId")
            info: dict[str, Any] = {}
            if kind in ("movie", "tv") and tmdb:
                try:
                    info = await self.get_json(f"/api/v1/{kind}/{int(tmdb)}")
                except Exception:  # noqa: BLE001
                    info = {}
            return {
                "id": str(r.get("id")), "kind": kind,
                "title": info.get("title") or info.get("name"),
                "year": (info.get("releaseDate") or info.get("firstAirDate") or "")[:4] or None,
                "poster_url": f"https://image.tmdb.org/t/p/w342{info['posterPath']}" if info.get("posterPath") else None,
                "status": self.REQUEST_STATUS.get(r.get("status"), "unknown"),
                "media_status": self.MEDIA_STATUS.get(media.get("status"), "unknown"),
                "requested_by": (r.get("requestedBy") or {}).get("displayName"),
                "requested_at": r.get("createdAt"),
            }

        return list(await asyncio.gather(*(title(r) for r in rows)))


def _hms(v: str | None) -> int | None:
    """Sonarr/Radarr timeleft ("1.02:03:04" or "02:03:04") in seconds."""
    if not v:
        return None
    days = 0
    if "." in v.split(":")[0]:
        d, v = v.split(".", 1)
        days = int(d)
    try:
        h, m, sec = (int(float(x)) for x in v.split(":"))
    except ValueError:
        return None
    return days * 86400 + h * 3600 + m * 60 + sec


class Arr(Adapter):
    """Sonarr or Radarr (v3 API): the download queue for the pipeline screen."""

    title = "Downloads"
    ping_path = "/api/v3/system/status"

    def __init__(self, name: str, cfg, vault):
        super().__init__(cfg, vault)
        self.name = name

    def auth_headers(self) -> dict[str, str]:
        key = self.secret(self.cfg.token_secret)
        return {"X-Api-Key": key} if key else {}

    async def queue(self) -> list[dict[str, Any]]:
        data = await self.get_json("/api/v3/queue", params={"pageSize": 50})
        out = []
        for rec in data.get("records", []):
            size, left = rec.get("size") or 0, rec.get("sizeleft") or 0
            row: dict[str, Any] = {
                "title": rec.get("title"), "progress": round(1 - left / size, 3) if size else None,
                "eta_s": None, "state": rec.get("trackedDownloadState") or rec.get("status"), "source": self.name,
            }
            left_s = _hms(rec.get("timeleft"))
            row["eta_s"] = left_s
            if rec.get("trackedDownloadStatus") in ("warning", "error"):
                row["error"] = "; ".join(m.get("title", "") for m in rec.get("statusMessages", [])) or "stalled"
            out.append(row)
        return out

    async def upcoming(self, days: int = 7) -> list[dict[str, Any]]:
        import datetime as dt

        start = dt.datetime.now(dt.timezone.utc)
        end = start + dt.timedelta(days=days)
        params: dict[str, Any] = {"start": start.date().isoformat(), "end": end.date().isoformat()}
        if self.name == "sonarr":
            params["includeSeries"] = "true"
        out = []
        for it in await self.get_json("/api/v3/calendar", params=params):
            if self.name == "sonarr":
                series = it.get("series") or {}
                out.append({"kind": "episode", "title": series.get("title"),
                            "detail": f"S{it.get('seasonNumber', 0):02d}E{it.get('episodeNumber', 0):02d} {it.get('title') or ''}".strip(),
                            "at": it.get("airDateUtc"), "has_file": bool(it.get("hasFile")), "source": self.name})
            else:
                at = it.get("digitalRelease") or it.get("physicalRelease") or it.get("inCinemas")
                out.append({"kind": "movie", "title": it.get("title"), "detail": str(it.get("year") or ""),
                            "at": at, "has_file": bool(it.get("hasFile")), "source": self.name})
        return out

    async def recent(self, limit: int = 15) -> list[dict[str, Any]]:
        """Recently imported (downloaded and added to the library)."""
        params: dict[str, Any] = {"pageSize": limit, "sortKey": "date", "sortDirection": "descending", "eventType": 3}
        params["includeSeries" if self.name == "sonarr" else "includeMovie"] = "true"
        if self.name == "sonarr":
            params["includeEpisode"] = "true"
        data = await self.get_json("/api/v3/history", params=params)
        out = []
        for rec in data.get("records", []):
            if self.name == "sonarr":
                series, ep = rec.get("series") or {}, rec.get("episode") or {}
                title = series.get("title") or rec.get("sourceTitle")
                detail = f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d} {ep.get('title') or ''}".strip() if ep else None
            else:
                movie = rec.get("movie") or {}
                title, detail = movie.get("title") or rec.get("sourceTitle"), str(movie.get("year") or "") or None
            out.append({"kind": "episode" if self.name == "sonarr" else "movie", "title": title, "detail": detail,
                        "at": rec.get("date"), "source": self.name})
        return out
