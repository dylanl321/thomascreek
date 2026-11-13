"""YouTube videos the kids add, fetched onto the NAS with yt-dlp.

Someone shares a link (a video, a playlist or a channel), or finds it in the
"Add from YouTube" wizard (search and previews are in ``yt_find.py``). The hub
looks it up, saves it as a *source*, and queues its videos:

* a video: just that one;
* a playlist: everything in it, up to ``playlist_max``;
* a channel: its newest ``channel_latest`` uploads, or as many as the wizard
  asked for (``latest``, up to ``channel_max`` for "the whole channel"); the
  Videos tab, so no Shorts or live streams;
* hand-picked videos (kind ``picks``): the ones chosen in a preview, never followed.

Each source may carry its own quality (``max_height``: 480, 720 or 1080); a
video is fetched at the best quality any of its sources asked for.

Playlists and channels that are *followed* are checked again every
``check_every_s`` and anything new is fetched. Live and upcoming streams, and
anything longer than ``max_duration_s``, are skipped. Age-restricted videos
need a YouTube login, which the hub doesn't have, so they fail on their own.

Files are MP4s an iPhone plays as they are (H.264 up to ``max_height``), saved
as ``<dir>/<video id>.mp4`` with a ``.jpg`` picture beside them. When the
folder grows past ``cap_gb`` the oldest watched videos go first, then the
oldest. A video a parent removed is remembered so a followed channel doesn't
bring it back; a blocked channel can't be added again.

There is no approval step (Dylan, 2026-11-12); parents get a note and can
remove or block. In the app each video is a media item with id ``yt-<id>``, so
the offline downloads in ``downloads.py`` handle them like films.

yt-dlp runs as ``python -m yt_dlp`` so the daily ``pip install -U`` takes
effect without a restart. YouTube needs a JavaScript runtime (Deno), which
the ``yt-dlp[default,deno]`` package brings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .db import new_id
from .yt_find import Finder, picture_url

log = logging.getLogger("hub.youtube")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
PLAYABLE = {"h264", "hevc"}


class YouTubeError(Exception):
    """A plain reason the app can show."""


def parse_link(text: str) -> dict[str, str]:
    """What a pasted or shared link points at: {kind, id, url}. Raises YouTubeError for anything else."""
    text = (text or "").strip()
    m = re.search(r"https?://\S+", text)  # the share sheet sometimes adds words around the link
    raw = m.group(0) if m else text
    if "://" not in raw:
        raw = "https://" + raw
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if host not in YT_HOSTS:
        raise YouTubeError("That isn't a YouTube link.")
    q = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("youtu.be") and parts and VIDEO_ID.match(parts[0]):
        return _video(parts[0])
    if parts[:1] == ["watch"] and VIDEO_ID.match((q.get("v") or [""])[0]):
        return _video(q["v"][0])  # a video watched inside a playlist is still that one video
    if parts[:1] in (["shorts"], ["live"], ["embed"], ["v"]) and len(parts) > 1 and VIDEO_ID.match(parts[1]):
        return _video(parts[1])
    if parts[:1] == ["playlist"] and q.get("list"):
        pid = q["list"][0]
        if not re.match(r"^[A-Za-z0-9_-]{10,64}$", pid):
            raise YouTubeError("That playlist link looks broken.")
        return {"kind": "playlist", "id": pid, "url": f"https://www.youtube.com/playlist?list={pid}"}
    if parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
        base = parts[:1] if parts[0].startswith("@") else parts[:2]
        if len(base) < 1 or (not parts[0].startswith("@") and len(parts) < 2):
            raise YouTubeError("That channel link looks broken.")
        path = "/".join(base)
        return {"kind": "channel", "id": path, "url": f"https://www.youtube.com/{path}/videos"}
    raise YouTubeError("Share a video, a playlist or a channel.")


def _video(vid: str) -> dict[str, str]:
    return {"kind": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}"}


def _thumb(info: dict[str, Any], avatar: bool = False) -> str | None:
    thumbs = info.get("thumbnails") or []
    if avatar:
        # A channel's thumbnails mix its banner and its round picture; the picture is square.
        sq = [t for t in thumbs if t.get("width") and t.get("width") == t.get("height")]
        if sq:
            return max(sq, key=lambda t: t["width"])["url"]
    good = [t for t in thumbs if t.get("url") and (t.get("width") or 0) <= 1280]
    return (good[-1] if good else (thumbs[-1] if thumbs else {})).get("url") or info.get("thumbnail")


class YouTube:
    def __init__(self, hub: Any, root: Path):
        self.hub = hub
        self.cfg = hub.config.youtube
        self.db = hub.db
        self.root = Path(self.cfg.dir) if self.cfg.dir else root
        self.current: dict[str, Any] | None = None  # the video being fetched, with progress
        self.failures: list[dict[str, Any]] = []
        self.version: str | None = None
        self.updated_at: float | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.find = Finder(self)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # -- running yt-dlp ------------------------------------------------------
    def _cmd(self) -> list[str]:
        return [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-progress",
                "--cache-dir", str(self.root / ".cache"), "--socket-timeout", "30"]

    async def _run(self, args: list[str], timeout: float = 120) -> str:
        proc = await asyncio.create_subprocess_exec(*self._cmd(), *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise YouTubeError("YouTube took too long to answer.") from None
        if proc.returncode != 0:
            raise YouTubeError(_plain_error(err.decode(errors="ignore")))
        return out.decode(errors="ignore")

    async def lookup(self, link: dict[str, str], latest: int | None = None) -> dict[str, Any]:
        """Title, channel, picture and the videos a source would bring, without fetching anything.
        ``latest`` asks a channel for more (or fewer) of its newest uploads than the usual number."""
        if link["kind"] == "video":
            info = json.loads(await self._run(["-J", "--no-playlist", link["url"]]))
            entries = [info]
        else:
            n = self.cfg.channel_latest if link["kind"] == "channel" else self.cfg.playlist_max
            if latest and link["kind"] == "channel":
                n = max(1, min(int(latest), self.cfg.channel_max))
            info = json.loads(await self._run(["-J", "--flat-playlist", "--playlist-end", str(n), link["url"]], 180))
            entries = [e for e in info.get("entries") or [] if e]
        videos = [v for v in (self._entry(e) for e in entries) if v]
        channel = info.get("channel") or info.get("uploader") or (entries[0].get("channel") if entries else None)
        title = info.get("title") or ""
        if link["kind"] == "channel":
            title = channel or re.sub(r"\s+-\s+Videos$", "", title)
        if link["kind"] == "playlist" and not info.get("entries"):
            raise YouTubeError("That playlist is empty or private.")
        return {
            "kind": link["kind"], "url": link["url"], "yt_id": info.get("channel_id") if link["kind"] == "channel" else info.get("id") or link["id"],
            "title": title[:200], "channel": channel, "channel_id": info.get("channel_id") or (entries[0].get("channel_id") if entries else None),
            "thumb": _thumb(info, avatar=link["kind"] == "channel") or (videos[0]["thumb"] if videos else None),
            "followers": info.get("channel_follower_count"), "videos": videos,
        }

    def _entry(self, e: dict[str, Any]) -> dict[str, Any] | None:
        vid = e.get("id")
        if not vid or not VIDEO_ID.match(vid):
            return None
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live") or e.get("is_live"):
            return None
        dur = e.get("duration")
        if dur and dur > self.cfg.max_duration_s:
            return None
        return {"id": vid, "title": (e.get("title") or vid)[:200], "duration_s": dur, "channel": e.get("channel") or e.get("uploader"),
                "channel_id": e.get("channel_id"), "thumb": _thumb(e), "uploaded": e.get("upload_date")}

    # -- sources -------------------------------------------------------------
    def blocked(self, channel_id: str | None) -> bool:
        return bool(channel_id) and self.db.one("SELECT 1 AS x FROM yt_blocked WHERE channel_id=?", [channel_id]) is not None

    async def add(self, text: str, follow: bool, person_id: str, latest: int | None = None,
                  quality: int | None = None) -> dict[str, Any]:
        """Add a video, playlist or channel. ``latest`` is how many of a channel's newest uploads to bring
        (``channel_max`` for the whole channel); ``quality`` caps the height of its videos."""
        link = parse_link(text)
        info = await self.lookup(link, latest)
        if self.blocked(info["channel_id"]) or any(self.blocked(v.get("channel_id")) for v in info["videos"][:1]):
            raise YouTubeError("A grown-up turned that channel off.")
        if not info["videos"]:
            raise YouTubeError("There's nothing there that can be saved (live streams and very long videos are skipped).")
        same = self.db.one("SELECT * FROM yt_sources WHERE kind=? AND yt_id=?", [info["kind"], info["yt_id"]])
        if same:
            if follow and not same["follow"] and info["kind"] != "video":
                self.db.update("yt_sources", "id", same["id"], {"follow": 1})
            if latest and latest > (same.get("latest") or 0):
                self.db.update("yt_sources", "id", same["id"], {"latest": int(latest)})
            if quality:
                self.db.update("yt_sources", "id", same["id"], {"max_height": int(quality)})
            self._queue(same["id"], info["videos"])
            return self.source_out(self.db.one("SELECT * FROM yt_sources WHERE id=?", [same["id"]]))  # type: ignore[arg-type]
        sid = new_id("yts")
        self.db.insert("yt_sources", {
            "id": sid, "kind": info["kind"], "url": info["url"], "yt_id": info["yt_id"], "title": info["title"],
            "channel": info["channel"], "channel_id": info["channel_id"], "thumb_url": info["thumb"],
            "added_by": person_id, "created_at": time.time(), "follow": int(follow and info["kind"] != "video"),
            "last_checked_at": time.time(), "latest": int(latest) if latest else None,
            "max_height": int(quality) if quality else None})
        self._queue(sid, info["videos"])
        asyncio.create_task(self._save_picture(f"src-{sid}", info["thumb"]))
        self._wake.set()
        return self.source_out(self.db.one("SELECT * FROM yt_sources WHERE id=?", [sid]))  # type: ignore[arg-type]

    async def add_picks(self, video_ids: list[str], person_id: str, quality: int | None = None,
                        title: str | None = None) -> dict[str, Any]:
        """Hand-picked videos (from a channel or playlist preview) as one source that isn't followed."""
        ids = [v for v in dict.fromkeys(video_ids) if VIDEO_ID.match(v)][:100]
        if not ids:
            raise YouTubeError("Pick at least one video.")
        videos = []
        for vid in ids:
            v = await self.find.video(vid)
            if v and not self.blocked(v.get("channel_id")):
                videos.append(v)
        if not videos:
            raise YouTubeError("A grown-up turned that channel off.")
        channels = {v.get("channel_id") for v in videos}
        one = videos[0]
        same_channel = len(channels) == 1 and one.get("channel_id")
        name = title or (f"{len(videos)} from {one['channel']}" if same_channel and one.get("channel") else f"{len(videos)} videos")
        sid = new_id("yts")
        self.db.insert("yt_sources", {
            "id": sid, "kind": "picks", "url": f"https://www.youtube.com/channel/{one['channel_id']}" if same_channel else one.get("url") or f"https://www.youtube.com/watch?v={one['id']}",
            "yt_id": sid, "title": name[:200], "channel": one.get("channel") if same_channel else None,
            "channel_id": one.get("channel_id") if same_channel else None, "thumb_url": one.get("thumb"),
            "added_by": person_id, "created_at": time.time(), "follow": 0, "last_checked_at": time.time(),
            "latest": None, "max_height": int(quality) if quality else None})
        self._queue(sid, videos)
        asyncio.create_task(self._save_picture(f"src-{sid}", one.get("thumb")))
        self._wake.set()
        return self.source_out(self.db.one("SELECT * FROM yt_sources WHERE id=?", [sid]))  # type: ignore[arg-type]

    def height_for(self, v: dict[str, Any]) -> int:
        """The best quality any of the video's sources asked for, never above the hub's limit."""
        hs = []
        for sid in v.get("source_ids") or []:
            row = self.db.one("SELECT max_height FROM yt_sources WHERE id=?", [sid])
            if row is not None:
                hs.append(row.get("max_height") or self.cfg.max_height)
        return min(max(hs) if hs else self.cfg.max_height, self.cfg.max_height)

    def _queue(self, source_id: str, videos: list[dict[str, Any]]) -> int:
        added = 0
        for v in videos:
            if self.blocked(v.get("channel_id")):
                continue
            row = self.db.one("SELECT * FROM yt_videos WHERE id=?", [v["id"]])
            if row:
                if source_id not in row["source_ids"]:
                    self.db.update("yt_videos", "id", v["id"], {"source_ids": [*row["source_ids"], source_id]})
                continue
            self.db.insert("yt_videos", {
                "id": v["id"], "source_ids": [source_id], "title": v["title"], "channel": v.get("channel"),
                "channel_id": v.get("channel_id"), "duration_s": v.get("duration_s"), "uploaded": v.get("uploaded"),
                "thumb_url": v.get("thumb"), "status": "queued", "added_at": time.time()})
            added += 1
        if added:
            self._wake.set()
        return added

    async def refresh(self, force: bool = False) -> int:
        """Look for new videos in followed playlists and channels that are due."""
        due = time.time() - self.cfg.check_every_s
        found = 0
        for s in self.db.all("SELECT * FROM yt_sources WHERE follow=1"):
            if not force and (s.get("last_checked_at") or 0) > due:
                continue
            try:
                info = await self.lookup({"kind": s["kind"], "id": s["yt_id"], "url": s["url"]}, s.get("latest"))
                found += self._queue(s["id"], info["videos"])
                self.db.update("yt_sources", "id", s["id"], {"last_checked_at": time.time(), "last_error": None})
            except YouTubeError as exc:
                self.db.update("yt_sources", "id", s["id"], {"last_checked_at": time.time(), "last_error": str(exc)[:200]})
        return found

    def remove_source(self, source_id: str) -> None:
        """Forget a source and the videos only it brought."""
        for v in self.db.all("SELECT * FROM yt_videos"):
            if source_id in v["source_ids"]:
                rest = [s for s in v["source_ids"] if s != source_id]
                if rest:
                    self.db.update("yt_videos", "id", v["id"], {"source_ids": rest})
                else:
                    self._delete_files(v["id"])
                    self.db.execute("DELETE FROM yt_videos WHERE id=?", [v["id"]])
        self.db.execute("DELETE FROM yt_sources WHERE id=?", [source_id])
        (self.root / f"src-{source_id}.jpg").unlink(missing_ok=True)

    def remove_video(self, video_id: str) -> None:
        """A parent took it away: delete the file, and remember it so a channel doesn't bring it back."""
        self._delete_files(video_id)
        self.db.update("yt_videos", "id", video_id, {"status": "removed", "file": None, "bytes": None})

    def block(self, channel_id: str, name: str | None, person_id: str | None) -> int:
        self.db.execute("INSERT OR REPLACE INTO yt_blocked (channel_id, name, blocked_by, blocked_at) VALUES (?,?,?,?)",
                        [channel_id, name, person_id, time.time()])
        gone = 0
        for s in self.db.all("SELECT id FROM yt_sources WHERE channel_id=?", [channel_id]):
            self.remove_source(s["id"])
            gone += 1
        for v in self.db.all("SELECT id FROM yt_videos WHERE channel_id=?", [channel_id]):
            self._delete_files(v["id"])
            self.db.execute("DELETE FROM yt_videos WHERE id=?", [v["id"]])
        return gone

    def unblock(self, channel_id: str) -> None:
        self.db.execute("DELETE FROM yt_blocked WHERE channel_id=?", [channel_id])

    # -- fetching ------------------------------------------------------------
    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        # A fetch the hub was in the middle of when it stopped starts again.
        self.db.execute("UPDATE yt_videos SET status='queued' WHERE status='fetching'")
        last_update = 0.0
        while True:
            try:
                if time.time() - last_update > 86400:
                    last_update = time.time()
                    if self.cfg.auto_update:
                        await self.update_ytdlp()
                    else:
                        self.version = (await self._run(["--version"], 30)).strip()
                await self.refresh()
                while await self.fetch_next():
                    pass
                self.trim()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("youtube loop: %s", exc)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    async def fetch_next(self) -> bool:
        v = self.db.one("SELECT * FROM yt_videos WHERE status='queued' ORDER BY added_at, uploaded DESC LIMIT 1")
        if v is None:
            return False
        await self.fetch(v)
        return True

    async def fetch(self, v: dict[str, Any]) -> None:
        vid = v["id"]
        self.root.mkdir(parents=True, exist_ok=True)
        self.db.update("yt_videos", "id", vid, {"status": "fetching", "error": None})
        self.current = {"id": vid, "title": v["title"], "progress": 0.0}
        h = self.height_for(v)
        fmt = (f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1][height<={h}]"
               f"/bv*[height<={h}]+ba/b[height<={h}]/b")
        args = ["-f", fmt, "--merge-output-format", "mp4", "--no-playlist", "--newline", "--progress",
                "--progress-template", "download:TCPROG %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
                "--write-thumbnail", "--convert-thumbnails", "jpg", "--no-part",
                "--match-filter", "!is_live & live_status!=is_upcoming",
                "-o", str(self.root / f"{vid}.%(ext)s"), "-o", f"thumbnail:{self.root / vid}.%(ext)s",
                f"https://www.youtube.com/watch?v={vid}"]
        cmd = [c for c in self._cmd() if c != "--no-progress"]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, *args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            assert proc.stdout is not None
            stream_no, last = 0, 0.0
            async for line in proc.stdout:
                parts = line.decode(errors="ignore").split()
                if len(parts) == 4 and parts[0] == "TCPROG" and parts[1].isdigit():
                    total = next((int(p) for p in parts[2:] if p.replace(".", "").isdigit() and float(p) > 0), 0)
                    f = min(1.0, int(parts[1]) / total) if total else 0.0
                    if f + 0.2 < last:  # the sound starts after the picture
                        stream_no += 1
                    last = f
                    # Picture is most of it; sound is the last bit.
                    self.current["progress"] = round(min(0.99, f * 0.9 if stream_no == 0 else 0.9 + f * 0.09), 3)
            err = (await proc.stderr.read()).decode(errors="ignore") if proc.stderr else ""
            code = await proc.wait()
            out = self.root / f"{vid}.mp4"
            if code != 0 or not out.exists():
                raise YouTubeError(_plain_error(err))
            probe = await _probe(out)
            if probe.get("video") not in PLAYABLE:
                self.current["progress"] = 0.99
                await _to_h264(out)
                probe = await _probe(out)
            if not (self.root / f"{vid}.jpg").exists():
                await self._save_picture(vid, v.get("thumb_url"))
            self.db.update("yt_videos", "id", vid, {
                "status": "ready", "file": out.name, "bytes": out.stat().st_size, "ready_at": time.time(),
                "duration_s": probe.get("duration") or v.get("duration_s"), "height": probe.get("height")})
            self.hub.bus.publish({"type": "youtube", "data": {"id": vid, "status": "ready"}})
        except asyncio.CancelledError:
            self.db.update("yt_videos", "id", vid, {"status": "queued"})
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, YouTubeError) else "Couldn't save it."
            log.warning("youtube %s failed: %s", vid, exc)
            self.failures = [*self.failures[-9:], {"at": time.time(), "video": vid, "error": str(exc)[:300]}]
            self.db.update("yt_videos", "id", vid, {"status": "failed", "error": msg[:200]})
            for f in self.root.glob(f"{vid}.*"):
                if f.suffix != ".jpg":
                    f.unlink(missing_ok=True)
        finally:
            self.current = None

    async def _save_picture(self, name: str, url: str | None) -> None:
        if not url or not url.startswith("https://"):
            return
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                self.root.mkdir(parents=True, exist_ok=True)
                data = r.content
                if not r.headers["content-type"].startswith("image/jpeg"):
                    data = await _jpeg(data)
                (self.root / f"{name}.jpg").write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("picture %s: %s", name, exc)

    def _delete_files(self, vid: str) -> None:
        for f in self.root.glob(f"{vid}.*"):
            f.unlink(missing_ok=True)

    def trim(self) -> int:
        """Keep the folder under the cap: oldest watched first, then oldest."""
        cap = int(self.cfg.cap_gb * 1_000_000_000)
        rows = self.db.all("SELECT id, bytes, watched_at, ready_at FROM yt_videos WHERE status='ready'")
        used = sum(r["bytes"] or 0 for r in rows)
        # Videos waiting to go to a kid's iPad (hub/sends.py) go last.
        sending = self.hub.sends.protected() if getattr(self.hub, "sends", None) else set()
        rows.sort(key=lambda r: (f"yt-{r['id']}" in sending, r["watched_at"] is None, r["watched_at"] or 0, r["ready_at"] or 0))
        gone = 0
        for r in rows:
            if used <= cap:
                break
            self._delete_files(r["id"])
            self.db.update("yt_videos", "id", r["id"], {"status": "trimmed", "file": None, "bytes": None})
            used -= r["bytes"] or 0
            gone += 1
        return gone

    async def update_ytdlp(self) -> None:
        """YouTube changes often and yt-dlp follows within days, so keep it current."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-q", "-U", "--no-cache-dir", "yt-dlp[default,deno]",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), 300)
        except Exception as exc:  # noqa: BLE001
            log.info("yt-dlp update: %s", exc)
        try:
            self.version = (await self._run(["--version"], 30)).strip()
            self.updated_at = time.time()
        except Exception:  # noqa: BLE001
            pass

    # -- reading -------------------------------------------------------------
    def video(self, vid: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM yt_videos WHERE id=?", [vid])

    def file(self, v: dict[str, Any]) -> Path | None:
        if v.get("status") != "ready" or not v.get("file"):
            return None
        p = self.root / v["file"]
        return p if p.exists() else None

    async def remote_picture(self, url: str) -> Path | None:
        """A YouTube thumbnail fetched through the hub (kids' devices may not reach YouTube's image hosts)."""
        import hashlib
        u = picture_url(url)
        if u is None:
            return None
        d = self.root / ".pictures"
        p = d / (hashlib.sha1(u.encode()).hexdigest() + ".jpg")
        if p.exists():
            return p
        d.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(u)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or not ctype.startswith("image/"):
                return None
            data = r.content if ctype.startswith("image/jpeg") else await _jpeg(r.content)
            p.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("remote picture %s: %s", u, exc)
            return None
        files = sorted(d.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        for f in files[:-2000]:
            f.unlink(missing_ok=True)
        return p

    def picture(self, name: str) -> Path | None:
        if not re.match(r"^(src-yts_[0-9a-f]{20}|[A-Za-z0-9_-]{11})$", name):
            return None
        p = self.root / f"{name}.jpg"
        return p if p.exists() else None

    def item(self, v: dict[str, Any]) -> dict[str, Any]:
        """A video as a media item, so the app's library, downloads and player use it like a film."""
        return {
            "id": f"yt-{v['id']}", "title": v["title"], "kind": "youtube", "year": int(v["uploaded"][:4]) if v.get("uploaded") else None,
            "poster_url": f"/v1/youtube/picture/{v['id']}", "rating": None, "summary": v.get("channel"),
            "show": v.get("channel"), "duration_s": int(v["duration_s"]) if v.get("duration_s") else None,
        }

    def video_out(self, v: dict[str, Any]) -> dict[str, Any]:
        out = {**self.item(v), "video_id": v["id"], "channel": v.get("channel"), "channel_id": v.get("channel_id"),
               "status": v["status"], "bytes": v.get("bytes"), "error": v.get("error"), "height": v.get("height"),
               "added_at": v.get("added_at"), "watched": bool(v.get("watched_at")), "source_ids": v["source_ids"]}
        if self.current and self.current["id"] == v["id"]:
            out["progress"] = self.current["progress"]
        return out

    def source_out(self, s: dict[str, Any]) -> dict[str, Any]:
        vids = [v for v in self.db.all("SELECT id, status, bytes, source_ids FROM yt_videos") if s["id"] in v["source_ids"]]
        person = self.db.person(s.get("added_by"))
        return {"id": s["id"], "kind": s["kind"], "title": s["title"], "channel": s.get("channel"), "channel_id": s.get("channel_id"),
                "url": s["url"], "follow": bool(s["follow"]), "added_by": person["name"] if person else None,
                "added_by_id": s.get("added_by"), "created_at": s["created_at"],
                "picture_url": f"/v1/youtube/picture/src-{s['id']}" if (self.root / f"src-{s['id']}.jpg").exists() else None,
                "count": sum(1 for v in vids if v["status"] == "ready"),
                "waiting": sum(1 for v in vids if v["status"] in ("queued", "fetching")),
                "bytes": sum(v["bytes"] or 0 for v in vids if v["status"] == "ready"),
                "last_error": s.get("last_error"), "latest": s.get("latest"), "quality": s.get("max_height")}

    def summary(self) -> dict[str, Any]:
        rows = self.db.all("SELECT status, bytes FROM yt_videos")
        return {"enabled": self.enabled, "sources": len(self.db.all("SELECT id FROM yt_sources")),
                "ready": sum(1 for r in rows if r["status"] == "ready"),
                "waiting": sum(1 for r in rows if r["status"] in ("queued", "fetching")),
                "failed": sum(1 for r in rows if r["status"] == "failed"),
                "bytes": sum(r["bytes"] or 0 for r in rows if r["status"] == "ready"),
                "cap_bytes": int(self.cfg.cap_gb * 1_000_000_000), "yt_dlp": self.version,
                "current": self.current, "failures": self.failures}

    def played(self, vid: str, offset_s: float, watched: bool) -> None:
        values: dict[str, Any] = {"position_s": offset_s}
        if watched:
            values["watched_at"] = time.time()
        self.db.update("yt_videos", "id", vid, values)


def _plain_error(err: str) -> str:
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    low = last.lower()
    if "sign in to confirm your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return "YouTube says this one is for grown-ups."
    if "private video" in low or "members-only" in low or "join this channel" in low:
        return "That video is private."
    if "unavailable" in low or "removed" in low or "does not exist" in low or "404" in low:
        return "YouTube says that isn't there any more."
    if "sign in to confirm you" in low or "bot" in low:
        return "YouTube is asking the hub to sign in. Try again later."
    if "premieres in" in low or "live event" in low or "is_live" in low:
        return "That's a live stream, which can't be saved."
    return "YouTube didn't give it to the hub. " + (re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", last)[:120])


async def _probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"video": "h264"}
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,height:format=duration",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        return {}
    st = (data.get("streams") or [{}])[0]
    dur = (data.get("format") or {}).get("duration")
    return {"video": st.get("codec_name"), "height": st.get("height"), "duration": float(dur) if dur else None}


async def _to_h264(path: Path) -> None:
    """YouTube had no H.264 copy (rare): convert so an iPhone can play it."""
    tmp = path.with_suffix(".h264.mp4")
    proc = await asyncio.create_subprocess_exec(
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-y", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise YouTubeError("Couldn't convert it for iPhone.")
    tmp.replace(path)


async def _jpeg(data: bytes) -> bytes:
    """Channel pictures are often WebP or PNG; the app caches JPEG."""
    proc = await asyncio.create_subprocess_exec(
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
        "-f", "image2", "-c:v", "mjpeg", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate(data)
    return out if proc.returncode == 0 and out else data
