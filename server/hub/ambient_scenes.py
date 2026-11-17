"""Ambiance scenes from YouTube for the TVs: a fireplace, a holiday cabin with jazz, rain on a window.

The hub keeps the catalog, so every TV and the iPhone see the same list:

* **Categories** ship with the hub (``CATEGORIES``). Each one has a fixed YouTube search, and the hub fills it
  with the top long videos every few days (``suggest_every_h``). Nobody types these searches on a TV, so a
  child sees only what these words find, minus anything a parent removed.
* **Added** scenes: a parent pastes a link or picks a search result on the iPhone and puts it in a category.
  Those come first in their category. Removing a suggestion hides it for good.
* **Saved clips**: a TV can't embed YouTube (tvOS has no web view), and an idle screen must loop quietly
  without ads or a second app. So the hub saves the first ``clip_min`` minutes of a scene to the NAS with the
  same yt-dlp the kids' YouTube uses, and the TVs loop that file. Added scenes, the season's top picks and
  the idle choice are saved on their own; any other scene is saved the first time someone plays it, and
  meanwhile the TV opens it in the YouTube app. ``save: false`` turns saving off and every scene opens in
  the YouTube app instead.
* **Idle**: a TV that nobody touched for ``after_min`` minutes shows the idle scene (the season's pick, or
  one a parent chose), sound off. House-wide, with an optional per-TV override.

Every outside call (YouTube search, the clip itself) is the hub's; TVs only get ``/v1/...`` paths.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .yt_find import FILTERS, _best, picture_path

log = logging.getLogger("hub.ambient")

KV = "tv.ambient.scenes"
KV_CLIPS = "tv.ambient.clips"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIN_SCENE_S = 15 * 60          # a suggestion shorter than this isn't an ambiance scene
PER_CATEGORY = 8
AUTO_SAVE = 2                  # the season's category saves its first two on its own
IDLE_CHOICES = ("season", "shuffle")
# Words that keep a search result out, for everyone. The searches are fixed, so this is a second net.
_NOT_FOR_US = re.compile(r"\b(sexy|bikini|lingerie|nsfw|18\+|horror|gore|jumpscare|scary|creepy|asmr girl|hot girl)\b", re.I)

CATEGORIES: tuple[dict[str, Any], ...] = (
    {"id": "holiday-cabin", "title": "Holiday cabin & jazz", "query": "christmas cabin ambience jazz fireplace snow",
     "seasons": ("winter",), "holidays": ("christmas",)},
    {"id": "fireplace", "title": "Fireplace", "query": "crackling fireplace 4k ambience no music",
     "seasons": ("winter", "autumn"), "holidays": ()},
    {"id": "snow", "title": "Snowy windows", "query": "snowfall window cozy ambience 4k",
     "seasons": ("winter",), "holidays": ()},
    {"id": "cafe", "title": "Coffee shop jazz", "query": "cozy coffee shop ambience jazz music",
     "seasons": ("winter", "spring", "summer", "autumn"), "holidays": ()},
    {"id": "autumn", "title": "Autumn cabin", "query": "autumn cabin rain fireplace ambience",
     "seasons": ("autumn",), "holidays": ()},
    {"id": "halloween", "title": "Halloween", "query": "cozy halloween ambience pumpkins autumn night",
     "seasons": ("autumn",), "holidays": ("halloween",)},
    {"id": "rain", "title": "Rain on the window", "query": "rain on window ambience 4k sleep",
     "seasons": ("spring", "autumn"), "holidays": ()},
    {"id": "forest", "title": "Forest & streams", "query": "forest stream nature ambience 4k birds",
     "seasons": ("spring", "summer"), "holidays": ()},
    {"id": "ocean", "title": "Beach & ocean", "query": "tropical beach ocean waves ambience 4k",
     "seasons": ("summer",), "holidays": ()},
    {"id": "aquarium", "title": "Aquarium", "query": "coral reef aquarium 4k relaxing",
     "seasons": ("winter", "spring", "summer", "autumn"), "holidays": ()},
)
_CATS = {c["id"]: c for c in CATEGORIES}


class SceneError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def video_id(text: object) -> str | None:
    """An 11-character id from an id or any YouTube link."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if VIDEO_ID.fullmatch(text):
        return text
    try:
        from .youtube import parse_link

        link = parse_link(text)
    except Exception:  # noqa: BLE001
        return None
    return link["id"] if link.get("kind") == "video" and VIDEO_ID.fullmatch(link.get("id") or "") else None


def season_category(moment: datetime) -> str:
    from .ambient import holiday_of, season_of

    holiday, season = holiday_of(moment), season_of(moment)
    if holiday:
        for c in CATEGORIES:
            if holiday in c["holidays"]:
                return c["id"]
    for c in CATEGORIES:
        if season in c["seasons"] and not c["holidays"] and c["id"] != "cafe":
            return c["id"]
    return "aquarium"


def _reason(moment: datetime) -> str:
    from .ambient import holiday_of, season_of

    holiday = holiday_of(moment)
    if holiday == "christmas":
        return "For the holidays"
    if holiday == "halloween":
        return "For Halloween"
    return {"winter": "For a winter evening", "spring": "For spring", "summer": "For summer",
            "autumn": "For autumn"}[season_of(moment)]


class Scenes:
    def __init__(self, hub: Any):
        self.hub = hub
        self.current: dict[str, Any] | None = None     # the clip being saved, with progress
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._refreshing = False

    # -- settings -------------------------------------------------------------
    @property
    def cfg(self) -> Any:
        return self.hub.config.ambient

    @property
    def root(self) -> Path:
        return Path(self.hub.youtube.root) / "ambient"

    def saving_on(self) -> bool:
        return bool(self.cfg.save) and bool(getattr(self.hub.youtube, "enabled", False))

    def _saved(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV) or {}
        return raw if isinstance(raw, dict) else {}

    def _store(self, saved: dict[str, Any]) -> None:
        self.hub.db.kv_set(KV, saved)

    def _clips(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV_CLIPS) or {}
        return raw if isinstance(raw, dict) else {}

    def _store_clips(self, clips: dict[str, Any]) -> None:
        self.hub.db.kv_set(KV_CLIPS, clips)

    # -- the catalog ----------------------------------------------------------
    def page(self, device: str | None, child: bool, can_edit: bool, now: datetime) -> dict[str, Any]:
        """Every category with its scenes, the season's pick first, and the idle settings for this TV."""
        self.maybe_refresh()
        saved, clips = self._saved(), self._clips()
        featured_cat = season_category(now)
        order = [featured_cat] + [c["id"] for c in CATEGORIES if c["id"] != featured_cat]
        categories = []
        for cid in order:
            items = [self._out(v, clips) for v in self._scenes(cid, saved, child)]
            if items:
                categories.append({"id": cid, "title": _CATS[cid]["title"], "items": items})
        featured = self._featured(categories)
        out: dict[str, Any] = {
            "featured": featured,
            "reason": _reason(now),
            "categories": categories,
            "idle": self.idle(device),
            "can_edit": can_edit,
            "saving_on": self.saving_on(),
        }
        if can_edit and self.current:
            out["saving"] = dict(self.current)
        return out

    def _featured(self, categories: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not categories:
            return None
        items = categories[0]["items"]
        return next((i for i in items if i["state"] == "ready"), items[0] if items else None)

    def _scenes(self, cid: str, saved: dict[str, Any], child: bool) -> list[dict[str, Any]]:
        hidden = set(saved.get("hidden") or [])
        added = [v for v in (saved.get("added") or {}).values() if isinstance(v, dict) and v.get("category") == cid]
        added.sort(key=lambda v: v.get("added_at") or 0)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for v in added:
            if v["id"] not in hidden and v["id"] not in seen:
                rows.append({**v, "added": True})
                seen.add(v["id"])
        for v in ((saved.get("suggested") or {}).get(cid) or {}).get("items") or []:
            if not isinstance(v, dict) or v.get("id") in hidden or v.get("id") in seen:
                continue
            if child and _NOT_FOR_US.search(str(v.get("title") or "")):
                continue
            rows.append({**v, "category": cid})
            seen.add(v["id"])
            if len(rows) >= PER_CATEGORY + len(added):
                break
        return rows

    def find(self, vid: str) -> dict[str, Any] | None:
        saved = self._saved()
        hit = (saved.get("added") or {}).get(vid)
        if isinstance(hit, dict):
            return {**hit, "added": True}
        for cid, block in (saved.get("suggested") or {}).items():
            for v in (block or {}).get("items") or []:
                if isinstance(v, dict) and v.get("id") == vid:
                    return {**v, "category": cid}
        return None

    def _out(self, v: dict[str, Any], clips: dict[str, Any]) -> dict[str, Any]:
        vid = v["id"]
        clip = clips.get(vid) or {}
        status = clip.get("status")
        item: dict[str, Any] = {
            "id": vid, "video_id": vid, "title": v.get("title") or vid, "category": v.get("category"),
            "youtube_url": f"https://www.youtube.com/watch?v={vid}",
        }
        for key in ("channel", "duration_s"):
            if v.get(key):
                item[key] = v[key]
        if v.get("picture"):
            item["picture_url"] = v["picture"]
        if v.get("added"):
            item["added"] = True
        if status == "ready" and self._file(vid) is not None:
            item["state"] = "ready"
            item["stream_url"] = f"/v1/ambient/videos/{vid}.mp4"
            if (self.root / f"{vid}.jpg").exists():
                item["picture_url"] = f"/v1/ambient/pictures/{vid}.jpg"
        elif self.current and self.current.get("id") == vid:
            item["state"] = "saving"
            item["progress"] = self.current.get("progress", 0.0)
        elif status == "queued":
            item["state"] = "queued"
        elif status == "failed":
            item["state"] = "failed"
            item["error"] = clip.get("error") or "YouTube didn't give it to the hub."
        else:
            item["state"] = "online"
        return item

    def item(self, vid: str) -> dict[str, Any] | None:
        v = self.find(vid)
        return self._out(v, self._clips()) if v else None

    # -- what parents change --------------------------------------------------
    async def add(self, text: str, category: str, person: str | None, title: str | None = None) -> dict[str, Any]:
        vid = video_id(text)
        if vid is None:
            raise SceneError(400, "bad_link", "That isn't a YouTube video link.")
        if category not in _CATS:
            raise SceneError(400, "bad_category", "Pick one of the categories.")
        found = self.find(vid)
        info: dict[str, Any] = {k: found[k] for k in ("title", "channel", "duration_s", "picture") if found and found.get(k)}
        if not info.get("title"):
            info.update(await self._lookup(vid))
        if title:
            info["title"] = title.strip()[:200]
        saved = self._saved()
        added = dict(saved.get("added") or {})
        added[vid] = {"id": vid, "category": category, "title": info.get("title") or vid, "channel": info.get("channel"),
                      "duration_s": info.get("duration_s"), "picture": info.get("picture"),
                      "added_by": person, "added_at": time.time()}
        saved["added"] = added
        saved["hidden"] = [h for h in saved.get("hidden") or [] if h != vid]
        self._store(saved)
        self.want(vid)
        return self.item(vid) or {}

    def remove(self, vid: str) -> None:
        if not VIDEO_ID.fullmatch(vid or ""):
            raise SceneError(404, "gone", "That scene isn't in the list.")
        saved = self._saved()
        added = dict(saved.get("added") or {})
        added.pop(vid, None)
        saved["added"] = added
        hidden = [h for h in saved.get("hidden") or [] if h != vid] + [vid]
        saved["hidden"] = hidden[-500:]
        idle = dict(saved.get("idle") or {})
        if idle.get("choice") == vid:
            idle["choice"] = "season"
            saved["idle"] = idle
        per = {d: ({**row, "choice": "season"} if row.get("choice") == vid else row)
               for d, row in (saved.get("idle_devices") or {}).items() if isinstance(row, dict)}
        saved["idle_devices"] = per
        self._store(saved)
        clips = self._clips()
        if clips.pop(vid, None) is not None:
            self._store_clips(clips)
        for f in self.root.glob(f"{vid}.*"):
            f.unlink(missing_ok=True)

    def idle(self, device: str | None = None) -> dict[str, Any]:
        saved = self._saved()
        base = {"enabled": True, "after_min": int(self.cfg.idle_after_min), "sound": False, "choice": "season"}
        base.update({k: v for k, v in (saved.get("idle") or {}).items() if k in base})
        if device:
            base.update({k: v for k, v in ((saved.get("idle_devices") or {}).get(device) or {}).items() if k in base})
        return base

    def set_idle(self, body: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise SceneError(400, "bad_request", "enabled is true or false.")
            clean["enabled"] = body["enabled"]
        if "sound" in body:
            if not isinstance(body["sound"], bool):
                raise SceneError(400, "bad_request", "sound is true or false.")
            clean["sound"] = body["sound"]
        if "after_min" in body:
            n = body["after_min"]
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 240:
                raise SceneError(400, "bad_request", "after_min is between 1 and 240 minutes.")
            clean["after_min"] = n
        if "choice" in body:
            choice = body["choice"]
            if choice not in IDLE_CHOICES and not (isinstance(choice, str) and self.find(choice)):
                raise SceneError(400, "bad_request", "Pick the season, shuffle, or a scene in the list.")
            clean["choice"] = choice
            if choice not in IDLE_CHOICES:
                self.want(choice)
        device = body.get("device")
        saved = self._saved()
        if isinstance(device, str) and device.strip() and len(device) <= 80 and "://" not in device:
            per = dict(saved.get("idle_devices") or {})
            if body.get("reset") is True:
                per.pop(device.strip(), None)
            else:
                per[device.strip()] = {**(per.get(device.strip()) or {}), **clean}
            saved["idle_devices"] = per
        else:
            saved["idle"] = {**(saved.get("idle") or {}), **clean}
            device = None
        self._store(saved)
        return self.idle(device)

    def idle_scene(self, device: str | None, now: datetime) -> dict[str, Any] | None:
        """The scene an idle TV loops: only a saved clip, because an idle screen can't open another app."""
        settings = self.idle(device)
        if not settings["enabled"]:
            return None
        clips = self._clips()
        choice = settings["choice"]
        if choice not in IDLE_CHOICES:
            v = self.find(choice)
            if v:
                out = self._out(v, clips)
                if out["state"] == "ready":
                    return out
        saved = self._saved()
        cid = season_category(now)
        pool = [self._out(v, clips) for v in self._scenes(cid, saved, child=True)]
        if choice == "shuffle":
            pool = [self._out(v, clips) for c in CATEGORIES for v in self._scenes(c["id"], saved, child=True)]
        ready = [i for i in pool if i["state"] == "ready"]
        if not ready:
            return None
        if choice == "shuffle":
            return ready[(now.timetuple().tm_yday * 24 + now.hour) % len(ready)]
        return ready[0]

    # -- search ---------------------------------------------------------------
    async def search(self, q: str) -> list[dict[str, Any]]:
        q = " ".join((q or "").split())[:100]
        if not q:
            return []
        direct = video_id(q)
        if direct:
            found = self.find(direct)
            info = found or {"id": direct, **(await self._lookup(direct))}
            return [self._out({**info, "id": direct}, self._clips())]
        rows = await self._search(q, 12, min_s=0)
        return [self._out(v, self._clips()) for v in rows]

    async def _search(self, q: str, limit: int, min_s: int = MIN_SCENE_S) -> list[dict[str, Any]]:
        yt = self.hub.youtube
        url = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={FILTERS['video']}"
        info = await yt.find._flat(url, limit + 6)
        out = []
        for e in info.get("entries") or []:
            v = _entry(e)
            if v is None or _NOT_FOR_US.search(v["title"]):
                continue
            if min_s and (v.get("duration_s") or 0) < min_s:
                continue
            if yt.blocked(e.get("channel_id")):
                continue
            out.append(v)
            if len(out) >= limit:
                break
        return out

    async def _lookup(self, vid: str) -> dict[str, Any]:
        try:
            raw = await self.hub.youtube._run(["-J", "--no-playlist", "--skip-download",
                                               f"https://www.youtube.com/watch?v={vid}"], 60)
            import json

            v = _entry(json.loads(raw)) or {}
            return {k: v[k] for k in ("title", "channel", "duration_s", "picture") if v.get(k)}
        except Exception as exc:  # noqa: BLE001
            log.info("ambient lookup %s: %s", vid, exc)
            return {}

    def maybe_refresh(self) -> None:
        """Fill stale categories in the background, so a page load never waits on YouTube."""
        if self._refreshing or not getattr(self.hub.youtube, "enabled", False):
            return
        if not self._stale():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refreshing = True
        loop.create_task(self._refresh_safely())

    def _stale(self) -> list[str]:
        sugg = self._saved().get("suggested") or {}
        limit = int(self.cfg.suggest_every_h) * 3600
        return [c["id"] for c in CATEGORIES if time.time() - float((sugg.get(c["id"]) or {}).get("at") or 0) > limit]

    async def _refresh_safely(self) -> None:
        try:
            await self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("ambient suggestions: %s", exc)
        finally:
            self._refreshing = False

    async def refresh(self, now: datetime | None = None) -> int:
        """Search each stale category. A search that fails keeps the old list and tries again next time."""
        filled = 0
        for cid in self._stale():
            try:
                rows = await self._search(_CATS[cid]["query"], PER_CATEGORY + 4)
            except Exception as exc:  # noqa: BLE001
                log.info("ambient search %s: %s", cid, exc)
                continue
            saved = self._saved()
            sugg = dict(saved.get("suggested") or {})
            old = (sugg.get(cid) or {}).get("items") or []
            sugg[cid] = {"at": time.time(), "items": rows or old}
            saved["suggested"] = sugg
            self._store(saved)
            filled += 1
        self.auto_save(now)
        return filled

    # -- saved clips ----------------------------------------------------------
    def want(self, vid: str) -> str:
        """Ask for a scene's clip. Returns its state; a no-op when it's there or on its way."""
        if not VIDEO_ID.fullmatch(vid or "") or not self.saving_on():
            return "online"
        clips = self._clips()
        clip = clips.get(vid) or {}
        if clip.get("status") == "ready" and self._file(vid):
            return "ready"
        if clip.get("status") in ("queued", "saving"):
            return str(clip["status"])
        if clip.get("status") == "failed" and time.time() - float(clip.get("at") or 0) < 6 * 3600:
            return "failed"
        clips[vid] = {"status": "queued", "at": time.time()}
        self._store_clips(clips)
        self._wake.set()
        return "queued"

    def auto_save(self, now: datetime | None = None) -> None:
        if not self.saving_on():
            return
        moment = self.hub.ambient._now(now)
        saved = self._saved()
        for v in self._scenes(season_category(moment), saved, child=True)[:AUTO_SAVE]:
            self.want(v["id"])
        for v in (saved.get("added") or {}).values():
            if isinstance(v, dict):
                self.want(v["id"])

    def _file(self, vid: str) -> Path | None:
        p = self.root / f"{vid}.mp4"
        return p if VIDEO_ID.fullmatch(vid or "") and p.exists() else None

    def file(self, vid: str) -> Path | None:
        clip = self._clips().get(vid) or {}
        return self._file(vid) if clip.get("status") == "ready" else None

    def picture(self, vid: str) -> Path | None:
        p = self.root / f"{vid}.jpg"
        return p if VIDEO_ID.fullmatch(vid or "") and p.exists() else None

    def played(self, vid: str) -> None:
        clips = self._clips()
        if vid in clips:
            clips[vid]["played_at"] = time.time()
            self._store_clips(clips)

    def start(self) -> None:
        if self._task is None and self.saving_on():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        clips = self._clips()
        for clip in clips.values():
            if clip.get("status") == "saving":
                clip["status"] = "queued"
        self._store_clips(clips)
        while True:
            try:
                if self._stale():
                    await self.refresh()
                else:
                    self.auto_save()
                while await self.save_next():
                    pass
                self.trim()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("ambient loop: %s", exc)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    async def save_next(self) -> bool:
        clips = self._clips()
        waiting = sorted((c.get("at") or 0, vid) for vid, c in clips.items() if c.get("status") == "queued")
        if not waiting:
            return False
        await self.save(waiting[0][1])
        return True

    async def save(self, vid: str) -> None:
        """The first clip_min minutes, H.264 MP4 both TVs play, with a picture beside it."""
        from .youtube import PLAYABLE, YouTubeError, _plain_error, _probe, _to_h264

        yt = self.hub.youtube
        self.root.mkdir(parents=True, exist_ok=True)
        self._set_clip(vid, status="saving", error=None)
        v = self.find(vid) or {}
        self.current = {"id": vid, "title": v.get("title") or vid, "progress": 0.0}
        h = int(self.cfg.clip_height)
        secs = int(self.cfg.clip_min) * 60
        fmt = (f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1][height<={h}]"
               f"/bv*[height<={h}]+ba/b[height<={h}]/b")
        args = ["-f", fmt, "--merge-output-format", "mp4", "--no-playlist", "--no-part",
                "--download-sections", f"*0-{secs}",
                "--write-thumbnail", "--convert-thumbnails", "jpg",
                "--match-filter", "!is_live & live_status!=is_upcoming",
                "-o", str(self.root / f"{vid}.%(ext)s"), "-o", f"thumbnail:{self.root / vid}.%(ext)s",
                f"https://www.youtube.com/watch?v={vid}"]
        try:
            proc = await asyncio.create_subprocess_exec(*yt._cmd(), *args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=3 * 3600)
            out = self.root / f"{vid}.mp4"
            if proc.returncode != 0 or not out.exists():
                raise YouTubeError(_plain_error((err or b"").decode(errors="ignore")))
            self.current["progress"] = 0.95
            probe = await _probe(out)
            if probe.get("video") not in PLAYABLE:
                await _to_h264(out)
            self._set_clip(vid, status="ready", bytes=out.stat().st_size, ready_at=time.time(), error=None)
            self.hub.bus.publish({"type": "ambient", "data": {"id": vid, "status": "ready"}})
        except asyncio.CancelledError:
            self._set_clip(vid, status="queued")
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, YouTubeError) else "Couldn't save it."
            log.warning("ambient clip %s failed: %s", vid, exc)
            self._set_clip(vid, status="failed", error=msg[:200], at=time.time())
            for f in self.root.glob(f"{vid}.*"):
                if f.suffix != ".jpg":
                    f.unlink(missing_ok=True)
        finally:
            self.current = None

    def _set_clip(self, vid: str, **fields: Any) -> None:
        clips = self._clips()
        clips[vid] = {**(clips.get(vid) or {}), **fields}
        self._store_clips(clips)

    def trim(self) -> int:
        """Under the cap: suggestions nobody played lately go first; added scenes and the idle choice go last."""
        cap = int(float(self.cfg.cap_gb) * 1_000_000_000)
        clips = self._clips()
        ready = [(vid, c) for vid, c in clips.items() if c.get("status") == "ready"]
        used = sum(int(c.get("bytes") or 0) for _, c in ready)
        if used <= cap:
            return 0
        saved = self._saved()
        keep = set((saved.get("added") or {}).keys())
        keep.add(str((saved.get("idle") or {}).get("choice")))
        ready.sort(key=lambda r: (r[0] in keep, r[1].get("played_at") or 0, r[1].get("ready_at") or 0))
        gone = 0
        for vid, c in ready:
            if used <= cap:
                break
            for f in self.root.glob(f"{vid}.*"):
                f.unlink(missing_ok=True)
            used -= int(c.get("bytes") or 0)
            clips.pop(vid, None)
            gone += 1
        self._store_clips(clips)
        return gone


def _entry(e: dict[str, Any]) -> dict[str, Any] | None:
    """A search result or a lookup, as a scene. Live streams can't be saved or looped, so they're left out."""
    if not isinstance(e, dict):
        return None
    vid = e.get("id")
    if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
        return None
    if e.get("live_status") in ("is_live", "is_upcoming", "post_live") or e.get("is_live"):
        return None
    dur = e.get("duration")
    picture = _best(e.get("thumbnails") or [])
    if picture is None and isinstance(e.get("thumbnail"), str):
        picture = picture_path(e["thumbnail"])
    return {"id": vid, "title": str(e.get("title") or vid)[:200],
            "channel": e.get("channel") or e.get("uploader"),
            "duration_s": int(dur) if isinstance(dur, (int, float)) and dur > 0 else None,
            "picture": picture}
