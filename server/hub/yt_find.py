"""Finding things on YouTube for the "Add from YouTube" wizard: search, previews, suggestions.

Search runs through yt-dlp's search pages, which the hub already has, so no
account or key is needed. Results come in three kinds (channels, playlists and
videos), each marked with what the family already has, and channels a parent
blocked are left out. Kids get the same search (Dylan, 2026-11-13).

When a YouTube Data API key is in the vault (``youtube.api_key``), channel
previews and results also get each channel's total video count, which yt-dlp
can't see. The key is optional; any API failure falls back to yt-dlp.

Answers are cached for ``CACHE_S`` so the wizard can go back and forth quickly,
and every video seen is remembered, so "pick videos" can add them without
looking each one up again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

log = logging.getLogger("hub.youtube.find")

CACHE_S = 30 * 60
API_KEY = "youtube.api_key"
API = "https://www.googleapis.com/youtube/v3"
# YouTube's own search filters ("sp"): channels, playlists, videos.
FILTERS = {"channel": "EgIQAg%3D%3D", "playlist": "EgIQAw%3D%3D", "video": "EgIQAQ%3D%3D"}
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
PICTURE_HOSTS = ("i.ytimg.com", "yt3.ggpht.com", "yt3.googleusercontent.com", "i9.ytimg.com")


def picture_url(url: str | None) -> str | None:
    """YouTube picture links come protocol-relative and sometimes with odd hosts; keep the ones we can proxy."""
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    return url if url.startswith("https://") and host in PICTURE_HOSTS else None


def picture_path(url: str | None) -> str | None:
    """A YouTube picture as a hub path (/v1/youtube/thumb/<token>), so devices fetch it through the hub."""
    u = picture_url(url)
    if u is None:
        return None
    return "/v1/youtube/thumb/" + base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")


def picture_from_token(token: str) -> str | None:
    try:
        return picture_url(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())
    except Exception:  # noqa: BLE001
        return None


def _best(thumbs: list[dict[str, Any]], square: bool = False, max_w: int = 900) -> str | None:
    ts = [t for t in thumbs or [] if t.get("url")]
    if square:
        sq = [t for t in ts if t.get("width") and t.get("width") == t.get("height")]
        ts = sq or [t for t in ts if t.get("id") == "avatar_uncropped"] or ts
    sized = [t for t in ts if (t.get("width") or 0) <= max_w]
    pick = max(sized, key=lambda t: t.get("width") or 0) if sized else (ts[-1] if ts else None)
    return picture_path(pick["url"]) if pick else None


def _banner(thumbs: list[dict[str, Any]]) -> str | None:
    wide = [t for t in thumbs or [] if t.get("url") and t.get("width") and t.get("height") and t["width"] > 3 * t["height"]]
    if not wide:
        return None
    return picture_path(min(wide, key=lambda t: abs(t["width"] - 1138))["url"])


class Finder:
    def __init__(self, yt: Any):
        self.yt = yt
        self.db = yt.db
        self._cache: dict[str, tuple[float, Any]] = {}
        self.seen: dict[str, dict[str, Any]] = {}   # video id -> entry, for "pick videos"

    # -- plumbing ------------------------------------------------------------
    async def _cached(self, key: str, make: Any) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        val = await make()
        if len(self._cache) > 300:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                self._cache.pop(k, None)
        self._cache[key] = (time.time(), val)
        return val

    async def _flat(self, url: str, n: int, timeout: float = 60) -> dict[str, Any]:
        return json.loads(await self.yt._run(["-J", "--flat-playlist", "--playlist-end", str(n), url], timeout))

    def _key(self) -> str | None:
        try:
            v = self.yt.hub.vault.get(API_KEY)
        except Exception:  # noqa: BLE001
            return None
        return str(v) if v else None

    async def _api(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        key = self._key()
        if not key:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/{path}", params={**params, "key": key})
            if r.status_code != 200:
                log.info("youtube api %s: %s", path, r.status_code)
                return None
            return r.json()
        except Exception as exc:  # noqa: BLE001
            log.info("youtube api %s: %s", path, exc)
            return None

    async def video_counts(self, channel_ids: list[str]) -> dict[str, int]:
        ids = [c for c in dict.fromkeys(channel_ids) if c and CHANNEL_ID.match(c)][:50]
        if not ids:
            return {}
        data = await self._api("channels", {"part": "statistics", "id": ",".join(ids)})
        out: dict[str, int] = {}
        for it in (data or {}).get("items") or []:
            n = (it.get("statistics") or {}).get("videoCount")
            if n is not None and str(n).isdigit():
                out[it["id"]] = int(n)
        return out

    # -- what the family has --------------------------------------------------
    def _have(self) -> dict[str, Any]:
        sources = self.db.all("SELECT id, kind, yt_id, channel_id, follow FROM yt_sources")
        saved = Counter(r["channel_id"] for r in self.db.all("SELECT channel_id FROM yt_videos WHERE status!='removed'") if r["channel_id"])
        return {
            "channel": {s["yt_id"]: s for s in sources if s["kind"] == "channel"},
            "playlist": {s["yt_id"]: s for s in sources if s["kind"] == "playlist"},
            "video_ids": {r["id"] for r in self.db.all("SELECT id FROM yt_videos WHERE status!='removed'")},
            "saved": saved,
        }

    def _mark(self, item: dict[str, Any], have: dict[str, Any]) -> dict[str, Any]:
        k = item["kind"]
        if k in ("channel", "playlist"):
            src = have[k].get(item["id"])
            item["source_id"] = src["id"] if src else None
            item["following"] = bool(src and src["follow"])
        if k == "channel":
            item["saved"] = have["saved"].get(item["id"], 0)
        if k == "video":
            item["have"] = item["id"] in have["video_ids"]
        return item

    # -- turning yt-dlp entries into results ---------------------------------
    def _channel(self, e: dict[str, Any]) -> dict[str, Any] | None:
        cid = e.get("channel_id") or e.get("id")
        if not cid or not CHANNEL_ID.match(cid):
            return None
        return {"kind": "channel", "id": cid, "title": (e.get("channel") or e.get("title") or "")[:200],
                "handle": e.get("uploader_id") if str(e.get("uploader_id") or "").startswith("@") else None,
                "followers": e.get("channel_follower_count"), "verified": bool(e.get("channel_is_verified")),
                "picture": _best(e.get("thumbnails") or [], square=True), "videos": None,
                "url": f"https://www.youtube.com/channel/{cid}"}

    def _playlist(self, e: dict[str, Any]) -> dict[str, Any] | None:
        pid = e.get("id")
        if not pid or not PLAYLIST_ID.match(pid) or pid.startswith("UC"):
            return None
        return {"kind": "playlist", "id": pid, "title": (e.get("title") or "")[:200], "channel": e.get("channel") or e.get("uploader"),
                "channel_id": e.get("channel_id"), "picture": _best(e.get("thumbnails") or []),
                "count": e.get("playlist_count"), "url": f"https://www.youtube.com/playlist?list={pid}"}

    def _video(self, e: dict[str, Any], channel: dict[str, Any] | None = None) -> dict[str, Any] | None:
        base = self.yt._entry(e)
        if base is None:
            return None
        ch = channel or {}
        v = {"kind": "video", "id": base["id"], "title": base["title"], "channel": base.get("channel") or ch.get("title"),
             "channel_id": base.get("channel_id") or ch.get("id"), "duration_s": base.get("duration_s"),
             "views": e.get("view_count"), "picture": _best(e.get("thumbnails") or []) or picture_path(base.get("thumb")),
             "url": f"https://www.youtube.com/watch?v={base['id']}"}
        self.seen[v["id"]] = {**base, "channel": v["channel"], "channel_id": v["channel_id"]}
        if len(self.seen) > 5000:
            for k in list(self.seen)[:1000]:
                self.seen.pop(k, None)
        return v

    @staticmethod
    def _safe(conv: Any, e: dict[str, Any]) -> dict[str, Any] | None:
        """One odd result shouldn't sink the rest."""
        try:
            return conv(e)
        except Exception as exc:  # noqa: BLE001
            log.info("skipping a result: %s", exc)
            return None

    # -- search ---------------------------------------------------------------
    async def search(self, q: str, kind: str = "all", limit: int = 8) -> dict[str, Any]:
        q = " ".join(q.split())[:100]
        kinds = list(FILTERS) if kind == "all" else [kind]
        limit = max(1, min(limit, 30))

        async def one(k: str) -> list[dict[str, Any]]:
            async def make() -> list[dict[str, Any]]:
                url = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={FILTERS[k]}"
                info = await self._flat(url, limit + 4)
                conv = {"channel": self._channel, "playlist": self._playlist, "video": self._video}[k]
                return [x for x in (self._safe(conv, e) for e in info.get("entries") or [] if e) if x]
            try:
                return await self._cached(f"s:{k}:{q.lower()}:{limit}", make)
            except Exception as exc:  # noqa: BLE001
                log.info("search %s %r: %s", k, q, exc)
                return []

        got = dict(zip(kinds, await asyncio.gather(*(one(k) for k in kinds))))
        have = self._have()
        out: dict[str, Any] = {"q": q}
        for k in FILTERS:
            items = [dict(x) for x in got.get(k, [])]
            items = [x for x in items if not self.yt.blocked(x["id"] if k == "channel" else x.get("channel_id"))]
            out[k + "s"] = [self._mark(x, have) for x in items][:limit]
        if out["channels"]:
            counts = await self.video_counts([c["id"] for c in out["channels"]])
            for c in out["channels"]:
                c["videos"] = counts.get(c["id"], c["videos"])
        return out

    # -- previews -------------------------------------------------------------
    async def channel(self, channel_id: str, recent: int = 12) -> dict[str, Any]:
        if not CHANNEL_ID.match(channel_id):
            from .youtube import YouTubeError
            raise YouTubeError("That channel link looks broken.")

        async def make() -> dict[str, Any]:
            base = f"https://www.youtube.com/channel/{channel_id}"
            vids, pls = await asyncio.gather(self._flat(f"{base}/videos", recent), self._flat(f"{base}/playlists", 30),
                                             return_exceptions=True)
            if isinstance(vids, BaseException):
                raise vids
            thumbs = vids.get("thumbnails") or []
            ch = {"kind": "channel", "id": channel_id, "title": (vids.get("channel") or vids.get("uploader") or
                                                               re.sub(r"\s+-\s+Videos$", "", vids.get("title") or ""))[:200],
                  "handle": vids.get("uploader_id") if str(vids.get("uploader_id") or "").startswith("@") else None,
                  "followers": vids.get("channel_follower_count"), "verified": bool(vids.get("channel_is_verified")),
                  "description": (vids.get("description") or "")[:600], "picture": _best(thumbs, square=True),
                  "banner": _banner(thumbs), "videos": None, "url": base}
            recent_v = [v for v in (self._safe(lambda x: self._video(x, ch), e) for e in vids.get("entries") or [] if e) if v]
            playlists = []
            if not isinstance(pls, BaseException):
                for e in pls.get("entries") or []:
                    p = self._safe(self._playlist, e or {})
                    if p:
                        p["channel"], p["channel_id"] = p["channel"] or ch["title"], channel_id
                        playlists.append(p)
            return {"channel": ch, "recent": recent_v, "playlists": playlists}

        data = await self._cached(f"c:{channel_id}:{recent}", make)
        out = {"channel": dict(data["channel"]), "recent": [dict(v) for v in data["recent"]], "playlists": [dict(p) for p in data["playlists"]]}
        counts = await self.video_counts([channel_id])
        out["channel"]["videos"] = counts.get(channel_id)
        have = self._have()
        self._mark(out["channel"], have)
        out["recent"] = [self._mark(v, have) for v in out["recent"]]
        out["playlists"] = [self._mark(p, have) for p in out["playlists"]]
        out["blocked"] = self.yt.blocked(channel_id)
        return out

    async def playlist(self, playlist_id: str) -> dict[str, Any]:
        if not PLAYLIST_ID.match(playlist_id):
            from .youtube import YouTubeError
            raise YouTubeError("That playlist link looks broken.")

        async def make() -> dict[str, Any]:
            info = await self._flat(f"https://www.youtube.com/playlist?list={playlist_id}", self.yt.cfg.playlist_max)
            entries = [e for e in info.get("entries") or [] if e]
            first = entries[0] if entries else {}
            p = {"kind": "playlist", "id": playlist_id, "title": (info.get("title") or "")[:200],
                 "channel": info.get("channel") or info.get("uploader") or first.get("channel"),
                 "channel_id": info.get("channel_id") or first.get("channel_id"),
                 "picture": _best(first.get("thumbnails") or []) or _best(info.get("thumbnails") or []),
                 "description": (info.get("description") or "")[:600],
                 "count": info.get("playlist_count") or len(entries), "url": f"https://www.youtube.com/playlist?list={playlist_id}"}
            return {"playlist": p, "videos": [v for v in (self._safe(self._video, e) for e in entries) if v]}

        data = await self._cached(f"p:{playlist_id}", make)
        have = self._have()
        return {"playlist": self._mark(dict(data["playlist"]), have), "videos": [self._mark(dict(v), have) for v in data["videos"]],
                "blocked": self.yt.blocked(data["playlist"].get("channel_id"))}

    async def video(self, video_id: str) -> dict[str, Any] | None:
        """One video's details, from what search and previews already saw, or a fresh look."""
        if video_id in self.seen:
            return self.seen[video_id]
        info = await self.yt.lookup({"kind": "video", "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"})
        return info["videos"][0] if info["videos"] else None

    # -- start page -----------------------------------------------------------
    def start(self, limit: int = 8) -> dict[str, Any]:
        rows = self.db.all("SELECT * FROM yt_sources ORDER BY created_at DESC LIMIT 60")
        recent, seen = [], set()
        for s in rows:
            key = s.get("channel_id") or s["id"]
            if key in seen:
                continue
            seen.add(key)
            recent.append(self.yt.source_out(s))
            if len(recent) >= limit:
                break
        have = self._have()
        followed = set(have["channel"])
        suggested: list[dict[str, Any]] = []
        # Channels the family keeps saving single videos or playlists from, but doesn't follow.
        names = {r["channel_id"]: r["channel"] for r in self.db.all("SELECT channel_id, channel FROM yt_videos WHERE channel_id IS NOT NULL")}
        for cid, n in have["saved"].most_common(12):
            if n < 2 or cid in followed or self.yt.blocked(cid) or not CHANNEL_ID.match(cid):
                continue
            suggested.append({"kind": "channel", "id": cid, "title": names.get(cid) or "Channel", "picture": None,
                              "reason": f"{n} of its videos are saved", "saved": n})
        # Then the starter list from the hub settings.
        for st in self.yt.cfg.starter:
            if len(suggested) >= limit:
                break
            if st.channel_id in followed or self.yt.blocked(st.channel_id) or any(s["id"] == st.channel_id for s in suggested):
                continue
            suggested.append({"kind": "channel", "id": st.channel_id, "title": st.name, "picture": None,
                              "reason": st.note or "On your starter list", "saved": have["saved"].get(st.channel_id, 0)})
        # Pictures we already know from an earlier preview.
        for s in suggested:
            hit = self._cache.get(f"c:{s['id']}:12")
            if hit:
                s["picture"] = hit[1]["channel"].get("picture")
                s["followers"] = hit[1]["channel"].get("followers")
        return {"recent": recent, "suggested": suggested[:limit], "api_key": self._key() is not None}

    async def warm(self, channel_ids: list[str]) -> None:
        """Look up suggested channels in the background so their pictures show next time."""
        for cid in channel_ids[:8]:
            if f"c:{cid}:12" in self._cache:
                continue
            try:
                await self.channel(cid)
            except Exception as exc:  # noqa: BLE001
                log.debug("warm %s: %s", cid, exc)
