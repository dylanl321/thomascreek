"""Films, shows and YouTube a grown-up sends to someone's iPad (usually a kid's).

A *send* says what should be on one person's devices:

* ``title``: one film or episode from Plex, or one saved YouTube video (``yt-<id>``);
* ``show``: a Plex show, with a scope: ``next`` (the next ``count`` episodes after the last one this
  person watched, topped up as they watch), ``last`` (the newest ``count`` episodes), ``season`` (one
  season, the newest if none is given) or ``all``;
* ``youtube``: a YouTube channel or playlist the hub follows, newest ``count`` videos;
* ``request``: something requested in Jellyseerr that isn't on Plex yet (``movie:<tmdb>`` /
  ``tv:<tmdb>``). It turns into a ``title`` or ``show`` once Plex has it.

From the sends the hub works out each person's *lineup*: the exact titles their devices should hold.
Devices fetch it (``GET /v1/sends/mine``), download what they're missing with the usual offline
download flow, delete sent titles that dropped out of it, and report what they hold
(``POST /v1/sends/inventory``) so grown-ups can see what's on each iPad.

To keep the iPad's work short (iOS gives a woken app about 30 seconds), the hub prepares files ahead of
time and keeps them for a week, then wakes the devices with a silent push when the lineup changes.
Apple rations silent pushes, so the app also checks on open and in background refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from .db import new_id

log = logging.getLogger("hub.sends")

KINDS = {"title", "show", "youtube", "request"}
SHOW_SCOPES = {"next", "last", "season", "all"}
PREPARED_KEEP_S = 7 * 86400
REFRESH_S = 15 * 60
MAX_ITEMS = 300  # per send: a whole long-running show is plenty


def prep_device(person_id: str) -> str:
    """The pretend device the hub prepares a person's sends under; real devices share its files."""
    return f"send:{person_id}"


class Sends:
    def __init__(self, hub: Any):
        self.hub = hub
        self.db = hub.db
        # person id -> (computed_at, lineup items, per-send status)
        self._cache: dict[str, tuple[float, list[dict[str, Any]], dict[str, dict[str, Any]]]] = {}
        self._episodes: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    # -- the sends themselves ----------------------------------------------
    def all(self, person_id: str | None = None) -> list[dict[str, Any]]:
        if person_id:
            return self.db.all("SELECT * FROM sends WHERE person_id=? ORDER BY created_at", [person_id])
        return self.db.all("SELECT * FROM sends ORDER BY created_at")

    def get(self, send_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM sends WHERE id=?", [send_id])

    def add(self, person_id: str, kind: str, ref: str, title: str, *, poster_url: str | None = None,
            scope: str = "one", count: int | None = None, season: int | None = None, quality: str = "high",
            created_by: str | None = None) -> dict[str, Any]:
        same = self.db.one("SELECT * FROM sends WHERE person_id=? AND kind=? AND ref=?", [person_id, kind, ref])
        values = {"title": title[:200], "poster_url": poster_url, "scope": scope, "count": count, "season": season,
                  "quality": quality, "last_error": None}
        if same:
            # Sending the same thing again changes how much of it goes, rather than adding a duplicate.
            self.db.update("sends", "id", same["id"], values)
            sid = same["id"]
        else:
            sid = new_id("snd")
            self.db.insert("sends", {"id": sid, "person_id": person_id, "kind": kind, "ref": ref, **values,
                                     "created_by": created_by, "created_at": time.time()})
        self.changed(person_id)
        return self.get(sid)  # type: ignore[return-value]

    def update(self, send_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        s = self.get(send_id)
        if s is None:
            return None
        self.db.update("sends", "id", send_id, values)
        self.changed(s["person_id"])
        return self.get(send_id)

    def remove(self, send_id: str) -> bool:
        s = self.get(send_id)
        if s is None:
            return False
        self.db.execute("DELETE FROM sends WHERE id=?", [send_id])
        self.changed(s["person_id"])
        return True

    def changed(self, person_id: str) -> None:
        self._cache.pop(person_id, None)
        self._wake.set()

    # -- watching ----------------------------------------------------------
    def watched(self, person_id: str, item_id: str, at: float | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO send_watched (person_id, item_id, watched_at) VALUES (?,?,?)",
                        [person_id, item_id, at or time.time()])
        if any(s["kind"] in ("show", "youtube") for s in self.all(person_id)):
            self.changed(person_id)

    def watched_ids(self, person_id: str) -> dict[str, float]:
        return {r["item_id"]: r["watched_at"] for r in
                self.db.all("SELECT item_id, watched_at FROM send_watched WHERE person_id=?", [person_id])}

    # -- devices reporting what they hold ------------------------------------
    def report(self, device: dict[str, Any], holdings: list[dict[str, Any]], free_bytes: int | None,
               total_bytes: int | None) -> None:
        person_id = device.get("person_id")
        self.db.execute(
            "INSERT INTO send_devices (device_id, person_id, holdings, free_bytes, total_bytes, reported_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET person_id=excluded.person_id, holdings=excluded.holdings, "
            "free_bytes=excluded.free_bytes, total_bytes=excluded.total_bytes, reported_at=excluded.reported_at",
            [device["id"], person_id, json.dumps(holdings[:1000]), free_bytes, total_bytes, time.time()])
        if person_id:
            seen = self.watched_ids(person_id)
            for h in holdings:
                if h.get("watched") and h.get("item_id") and h["item_id"] not in seen:
                    self.watched(person_id, h["item_id"])

    def device_reports(self, person_id: str) -> dict[str, dict[str, Any]]:
        return {r["device_id"]: r for r in self.db.all("SELECT * FROM send_devices WHERE person_id=?", [person_id])}

    # -- working out the lineup -----------------------------------------------
    async def lineup(self, person_id: str, fresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """(items the person's devices should hold, status per send). Cached until something changes."""
        hit = self._cache.get(person_id)
        if hit and not fresh and time.time() - hit[0] < REFRESH_S:
            return hit[1], hit[2]
        lock = self._locks.setdefault(person_id, asyncio.Lock())
        async with lock:
            hit = self._cache.get(person_id)
            if hit and not fresh and time.time() - hit[0] < 5:
                return hit[1], hit[2]
            items, status = await self._compute(person_id)
            self._cache[person_id] = (time.time(), items, status)
            return items, status

    def cached_ids(self, person_id: str | None) -> set[str]:
        hit = self._cache.get(person_id or "")
        return {i["item_id"] for i in hit[1]} if hit else set()

    async def allowed(self, person_id: str | None, item_id: str) -> bool:
        """A grown-up sent this to that person (so their usual ratings limit doesn't apply)."""
        if not person_id or not self.all(person_id):
            return False
        if item_id in self.cached_ids(person_id):
            return True
        items, _ = await self.lineup(person_id)
        return any(i["item_id"] == item_id for i in items)

    def protected(self) -> set[str]:
        return {i["item_id"] for hit in self._cache.values() for i in hit[1]}

    async def _compute(self, person_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        watched = self.watched_ids(person_id)
        out: list[dict[str, Any]] = []
        status: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for s in self.all(person_id):
            try:
                if s["kind"] == "request":
                    await self._resolve_request(s)
                    s = self.get(s["id"]) or s
                items, st = await self._items_for(s, watched)
                if s.get("last_error"):
                    self.db.update("sends", "id", s["id"], {"last_error": None})
            except Exception as exc:  # noqa: BLE001 (one broken send shouldn't stop the others)
                log.info("send %s: %s", s["id"], exc)
                old = self._cache.get(person_id)
                items = [i for i in (old[1] if old else []) if i["send_id"] == s["id"]]
                st = {"state": "error", "detail": "Couldn't check it just now. The iPad keeps what it has."}
                self.db.update("sends", "id", s["id"], {"last_error": str(exc)[:200]})
            who = self.db.person(s.get("created_by"))
            for i in items:
                if i["item_id"] in seen:
                    continue
                seen.add(i["item_id"])
                out.append({**i, "send_id": s["id"], "quality": "original" if i["item_id"].startswith("yt-") else s["quality"],
                            "from": who["name"].split()[0] if who else None, "sent_at": s["created_at"],
                            "watched": i["item_id"] in watched})
            status[s["id"]] = {**st, "item_ids": [i["item_id"] for i in items]}
        return out, status

    async def _items_for(self, s: dict[str, Any], watched: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        h = self.hub
        if s["kind"] == "request":
            return [], {"state": "waiting", "detail": "Waiting for it to reach Plex"}
        if s["kind"] == "title":
            if s["ref"].startswith("yt-"):
                v = h.youtube.video(s["ref"][3:])
                if v is None or v["status"] in ("removed", "trimmed"):
                    return [], {"state": "gone", "detail": "That video is no longer on the hub"}
                ready = h.youtube.file(v) is not None
                return [{**_item(h.youtube.item(v)), "ready": ready}], {
                    "state": "ready" if ready else "preparing", "detail": None if ready else "The hub is still saving it"}
            item = await h.plex.metadata(s["ref"])
            if item is None:
                return [], {"state": "gone", "detail": "It's no longer on Plex"}
            return [{**_item(item), "ready": True}], {"state": "ready"}
        if s["kind"] == "show":
            eps = await self._show_episodes(s["ref"])
            pick = pick_episodes(eps, s["scope"], s.get("count"), s.get("season"), watched)
            detail = {"next": "Next up for them, topped up as they watch", "last": "The newest episodes",
                      "season": f"Season {pick[0]['season']}" if pick else "That season", "all": "Every episode"}[s["scope"]]
            if not pick:
                return [], {"state": "done" if s["scope"] == "next" and eps else "empty",
                            "detail": "They've watched every episode" if s["scope"] == "next" and eps else "No episodes found"}
            return [{**_item(e), "ready": True} for e in pick[:MAX_ITEMS]], {"state": "ready", "detail": detail}
        if s["kind"] == "youtube":
            src = self.db.one("SELECT * FROM yt_sources WHERE id=?", [s["ref"]])
            if src is None:
                return [], {"state": "gone", "detail": "That channel was removed from YouTube on the hub"}
            vids = [v for v in self.db.all("SELECT * FROM yt_videos") if s["ref"] in v["source_ids"]
                    and v["status"] not in ("removed", "failed")]
            vids.sort(key=lambda v: (v.get("uploaded") or "", v.get("added_at") or 0), reverse=True)
            vids = vids[: s.get("count") or 10]
            items = [{**_item(h.youtube.item(v)), "ready": h.youtube.file(v) is not None} for v in vids
                     if v["status"] != "trimmed" or f"yt-{v['id']}" in self.cached_ids(s["person_id"])]
            waiting = sum(1 for i in items if not i["ready"])
            return items, {"state": "ready" if not waiting else "preparing",
                           "detail": f"Newest {len(items)}" + (f", {waiting} still saving on the hub" if waiting else "")}
        return [], {"state": "error", "detail": "Unknown kind"}

    async def _show_episodes(self, show_id: str) -> list[dict[str, Any]]:
        hit = self._episodes.get(show_id)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        show = await self.hub.plex.metadata(show_id)
        eps = await self.hub.plex.episodes(show_id)
        for e in eps:
            e["rating"] = e.get("rating") or (show or {}).get("rating")
        eps.sort(key=lambda e: (e.get("season") or 0, e.get("episode") or 0))
        self._episodes[show_id] = (time.time(), eps)
        return eps

    async def _resolve_request(self, s: dict[str, Any]) -> None:
        """A requested title that has reached Plex becomes a normal film or show send."""
        h = self.hub
        kind, _, tmdb = s["ref"].partition(":")
        if not h.jellyseerr.enabled:
            return
        d = await h.jellyseerr.details(kind, int(tmdb), fresh=True)
        mi = d.get("mediaInfo") or {}
        key = mi.get("ratingKey")
        if not key or mi.get("status") not in (4, 5):
            return
        new_kind = "show" if kind == "tv" else "title"
        scope = s["scope"] if new_kind == "show" and s["scope"] in SHOW_SCOPES else ("all" if new_kind == "show" else "one")
        self.db.update("sends", "id", s["id"], {"kind": new_kind, "ref": str(key), "scope": scope})
        h.db.log("sends.arrived", "ok", person_id=s["person_id"], target=str(key), params={"title": s["title"]}, source="hub")

    # -- preparing files and waking devices ----------------------------------
    async def prepare(self, person_id: str, items: list[dict[str, Any]]) -> int:
        """Start the hub's side of each Plex title so the iPad only has to download. Returns jobs started."""
        h = self.hub
        started = 0
        have = {(j.item_id, j.quality): j for j in h.downloads.jobs.values() if j.status != "failed"}
        for i in items:
            iid, q = i["item_id"], i["quality"]
            if iid.startswith("yt-") or not i.get("ready"):
                continue
            j = have.get((iid, q))
            if j is not None:
                h.downloads.wanted(j.id, PREPARED_KEEP_S)
                continue
            try:
                media = await h.plex.media_info(iid)
            except Exception:  # noqa: BLE001
                continue
            if media is None:
                continue
            from .downloads import original_mode

            quality = q if (q != "original" or original_mode(media)) else "high"
            job = await h.downloads.start(iid, quality, media, {k: i.get(k) for k in ("id", "title", "kind", "poster_url", "rating")},
                                          prep_device(person_id), person_id)
            job.keep_s = PREPARED_KEEP_S
            have[(iid, quality)] = job
            started += 1
        return started

    def signature(self, items: list[dict[str, Any]]) -> str:
        key = sorted(f"{i['item_id']}:{i['quality']}:{int(bool(i.get('ready')))}" for i in items)
        return hashlib.sha1("|".join(key).encode()).hexdigest()[:16]

    async def wake_devices(self, person_id: str) -> int:
        """A silent push so the app wakes, fetches its lineup and hands downloads to iOS."""
        apns = self.hub.apns
        if not apns.enabled:
            return 0
        sent = 0
        for d in self.db.all("SELECT * FROM devices WHERE person_id=? AND status='active' AND apns_token IS NOT NULL",
                             [person_id]):
            res = await apns.send(d["apns_token"], {"aps": {"content-available": 1}, "cat": "sends"},
                                  env=d.get("apns_env") or "production", push_type="background", priority=5,
                                  collapse_id="sends")
            if res.token_dead:
                self.db.update("devices", "id", d["id"], {"apns_token": None})
            sent += int(res.ok)
        return sent

    async def sync(self, person_id: str, force_push: bool = False) -> dict[str, Any]:
        items, _ = await self.lineup(person_id, fresh=True)
        prepared = await self.prepare(person_id, items)
        sig = self.signature(items)
        key = f"sends.sig.{person_id}"
        pushed = 0
        if force_push or self.db.kv_get(key) != sig:
            self.db.kv_set(key, sig)
            pushed = await self.wake_devices(person_id)
        return {"items": len(items), "prepared": prepared, "pushed": pushed}

    def people_with_sends(self) -> list[str]:
        return [r["person_id"] for r in self.db.all("SELECT DISTINCT person_id FROM sends")]

    # -- background loop -----------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            self._wake.clear()
            for pid in self.people_with_sends():
                try:
                    await self.sync(pid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("sends sync for %s: %s", pid, exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=REFRESH_S)
                await asyncio.sleep(3)  # let a burst of changes settle
            except asyncio.TimeoutError:
                pass


def _item(i: dict[str, Any]) -> dict[str, Any]:
    return {"item_id": i["id"], "title": i.get("title"), "kind": i.get("kind"), "poster_url": i.get("poster_url"),
            "rating": i.get("rating"), "summary": i.get("summary"), "year": i.get("year"), "show": i.get("show"),
            "season": i.get("season"), "episode": i.get("episode"), "duration_s": i.get("duration_s")}


def pick_episodes(eps: list[dict[str, Any]], scope: str, count: int | None, season: int | None,
                  watched: dict[str, float]) -> list[dict[str, Any]]:
    """Which of a show's episodes (in order) a send covers."""
    n = count or 10
    if scope == "all":
        return list(eps)
    if scope == "season":
        seasons = sorted({e.get("season") or 0 for e in eps})
        want = season if season is not None else (max(s for s in seasons if s) if any(seasons) else 0)
        return [e for e in eps if (e.get("season") or 0) == want]
    if scope == "last":
        return eps[-n:]
    # next: the episodes after the furthest one this person has watched.
    last = -1
    for idx, e in enumerate(eps):
        if e["id"] in watched:
            last = idx
    return eps[last + 1: last + 1 + n]
