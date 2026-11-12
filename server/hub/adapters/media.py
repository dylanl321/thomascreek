"""Plex (browse, continue watching, artwork) and Jellyseerr (requests)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import Adapter


class Plex(Adapter):
    name = "plex"
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


class Arr(Adapter):
    """Sonarr or Radarr (v3 API): the download queue for the pipeline screen."""

    title = "Downloads"

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
            if rec.get("trackedDownloadStatus") in ("warning", "error"):
                row["error"] = "; ".join(m.get("title", "") for m in rec.get("statusMessages", [])) or "stalled"
            out.append(row)
        return out
