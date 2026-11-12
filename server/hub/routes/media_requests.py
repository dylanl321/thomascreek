"""Media requests: ask for a film or show, follow it to Plex, see what's there.

Jellyseerr is the source of truth, so requests made on its website or from a
Plex watchlist show up too. Nobody needs approval (Dylan, 2026-11-12); children
can only ask for titles with an allowed rating. Download progress comes from
Jellyseerr's own Sonarr/Radarr sync (media.downloadStatus).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..db import new_id
from ..state import Hub
from .deps import Caller, caller, error, hub, need

log = logging.getLogger("hub.media_requests")
router = APIRouter(prefix="/v1", tags=["media requests"])

TMDB_IMG = "https://image.tmdb.org/t/p/w342"
ACTIVE = ("waiting", "searching", "downloading", "partial", "failed")
READY = ("ready", "partial")
STATUS_TEXT = {
    "waiting": "Waiting for approval", "searching": "Looking for a copy", "downloading": "Downloading",
    "partial": "Partly on Plex", "ready": "Ready to watch", "failed": "Couldn't be added",
    "declined": "Declined", "removed": "Removed from Plex",
}


# -- shaping -------------------------------------------------------------------
def _year(d: dict[str, Any]) -> str | None:
    return (d.get("releaseDate") or d.get("firstAirDate") or "")[:4] or None


def _poster(d: dict[str, Any]) -> str | None:
    return f"{TMDB_IMG}{d['posterPath']}" if d.get("posterPath") else None


def _when(v: Any) -> float | None:
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _progress(dls: list[dict[str, Any]]) -> tuple[float | None, int | None]:
    size = sum(float(d.get("size") or 0) for d in dls)
    left = sum(float(d.get("sizeLeft") or 0) for d in dls)
    pct = round(1 - left / size, 3) if size else None
    etas = [_when(d.get("estimatedCompletionTime")) for d in dls]
    etas = [e for e in etas if e]
    eta = max(0, int(max(etas) - time.time())) if etas else None
    return pct, eta


def _season_status(n: int, media: dict[str, Any], dls: list[dict[str, Any]]) -> str:
    if any(((d.get("episode") or {}).get("seasonNumber")) == n for d in dls):
        return "downloading"
    ms = next((s.get("status") for s in media.get("seasons") or [] if s.get("seasonNumber") == n), None)
    return {5: "ready", 4: "partial"}.get(ms, "searching")


def derive(r: dict[str, Any]) -> dict[str, Any]:
    """Status, progress and per-season detail of one Jellyseerr request."""
    media = r.get("media") or {}
    rs, ms = r.get("status"), media.get("status")
    dls = media.get("downloadStatus") or []
    seasons = [{"n": s.get("seasonNumber"), "status": _season_status(s.get("seasonNumber"), media, dls)}
               for s in sorted(r.get("seasons") or [], key=lambda s: s.get("seasonNumber") or 0)]
    if rs == 3:
        status = "declined"
    elif seasons and all(s["status"] == "ready" for s in seasons):
        status = "ready"
    elif dls:
        status = "downloading"
    elif ms == 5 or (rs == 5 and not seasons):
        status = "ready"
    elif ms == 4 or (seasons and any(s["status"] in ("ready", "partial") for s in seasons)):
        status = "partial"
    elif rs == 4:
        status = "failed"
    elif rs == 1:
        status = "waiting"
    elif ms in (6, 7) and rs == 5:
        status = "removed"
    else:
        status = "searching"
    out: dict[str, Any] = {"status": status, "seasons": seasons}
    if dls:
        out["progress"], out["eta_s"] = _progress(dls)
        if seasons:
            eps = [d.get("episode") for d in dls if d.get("episode")]
            if eps:
                out["detail"] = f"{len(eps)} episode{'s' if len(eps) != 1 else ''} downloading"
    if status == "partial" and seasons:
        have = [s["n"] for s in seasons if s["status"] == "ready"]
        if have:
            out["detail"] = "Season " + ", ".join(str(n) for n in have) + " on Plex"
    if status == "searching" and r.get("updatedAt"):
        out["detail"] = "Sonarr and Radarr keep looking"
    if status == "failed":
        out["detail"] = "Sonarr or Radarr refused it. Try again."
    return out


def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0].split("@")[0].lower()


class Owners:
    """Who asked: the hub's own record when the app made it, otherwise Jellyseerr's user matched by name."""

    def __init__(self, h: Hub):
        self.people = {p["id"]: p for p in h.db.people()}
        self.by_name = {_first(p["name"]): p for p in self.people.values() if p.get("name")}
        self.rows = {r["external_id"]: r for r in h.db.all(
            "SELECT * FROM requests WHERE kind='media' AND external_id IS NOT NULL")}

    def person(self, r: dict[str, Any]) -> dict[str, Any] | None:
        row = self.rows.get(str(r.get("id")))
        if row:
            return self.people.get(row["requester_id"])
        by = r.get("requestedBy") or {}
        for n in (by.get("displayName"), by.get("plexUsername"), by.get("username"), by.get("email")):
            if n and _first(n) in self.by_name:
                return self.by_name[_first(n)]
        return None

    def via(self, r: dict[str, Any]) -> str | None:
        if str(r.get("id")) in self.rows:
            return None
        by = r.get("requestedBy") or {}
        return "Plex watchlist" if r.get("isAutoRequest") else ("Jellyseerr" if by else None)


async def _titles(h: Hub, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sem = asyncio.Semaphore(6)

    async def one(kind: str, tmdb: int) -> tuple[str, dict[str, Any]]:
        async with sem:
            try:
                return f"{kind}:{tmdb}", await h.jellyseerr.details(kind, tmdb)
            except (AdapterError, ValueError):
                return f"{kind}:{tmdb}", {}

    want = {(m.get("mediaType") or r.get("type"), m.get("tmdbId")) for r in rows
            for m in [r.get("media") or {}] if m.get("tmdbId")}
    return dict(await asyncio.gather(*(one(k, int(t)) for k, t in want if k in ("movie", "tv"))))


def shape(r: dict[str, Any], info: dict[str, Any], owners: Owners, me: str | None) -> dict[str, Any]:
    media = r.get("media") or {}
    kind = media.get("mediaType") or r.get("type")
    person = owners.person(r)
    by = (r.get("requestedBy") or {}).get("displayName")
    out = {
        "id": str(r.get("id")), "media_id": f"{kind}:{media.get('tmdbId')}", "kind": kind,
        "title": info.get("title") or info.get("name") or "Unknown title", "year": _year(info),
        "poster_url": _poster(info), "who": person["name"] if person else by,
        "person_id": person["id"] if person else None, "mine": bool(person and person["id"] == me),
        "via": owners.via(r), "created_at": r.get("createdAt"), "updated_at": r.get("updatedAt"),
        "plex_id": str(media["ratingKey"]) if media.get("ratingKey") else None,
        **derive(r),
    }
    out["status_text"] = STATUS_TEXT.get(out["status"], out["status"].capitalize())
    return out


async def build_list(h: Hub, me: str | None, take: int = 60) -> list[dict[str, Any]]:
    rows = await h.jellyseerr.all_requests(take=take)
    titles = await _titles(h, rows)
    owners = Owners(h)
    out = []
    for r in rows:
        m = r.get("media") or {}
        out.append(shape(r, titles.get(f"{m.get('mediaType') or r.get('type')}:{m.get('tmdbId')}", {}), owners, me))
    return out


def _childlike(c: Caller) -> bool:
    return c.subject.role not in ("admin", "adult")


def _avail(mi: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """What's already happening with a title: ready, partial, downloading, requested or nothing."""
    if not mi:
        return None, None
    reqs = sorted(mi.get("requests") or [], key=lambda r: r.get("id") or 0)
    rid = str(reqs[-1]["id"]) if reqs else None
    st = mi.get("status")
    if st == 5:
        return "ready", rid
    if mi.get("downloadStatus"):
        return "downloading", rid
    if st == 4:
        return "partial", rid
    if st in (2, 3):
        return "requested", rid
    return None, rid


def _result(r: dict[str, Any]) -> dict[str, Any]:
    state, rid = _avail(r.get("mediaInfo"))
    return {"media_id": f"{r['mediaType']}:{r['id']}", "kind": r["mediaType"], "title": r.get("title") or r.get("name"),
            "year": _year(r), "poster_url": _poster(r), "summary": r.get("overview"), "availability": state,
            "request_id": rid, "plex_id": str((r.get("mediaInfo") or {}).get("ratingKey") or "") or None}


async def _allowed_for_child(h: Hub, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ok = set(h.config.child_policy.ratings)
    sem = asyncio.Semaphore(6)

    async def rate(i: dict[str, Any]) -> dict[str, Any] | None:
        kind, tmdb = i["media_id"].split(":", 1)
        async with sem:
            try:
                d = await h.jellyseerr.details(kind, int(tmdb))
            except (AdapterError, ValueError):
                return None
        rating = h.jellyseerr.content_rating(kind, d)
        return {**i, "rating": rating} if rating in ok else None

    return [x for x in await asyncio.gather(*(rate(i) for i in items[:16])) if x]


def _need_seerr(h: Hub) -> None:
    if not h.jellyseerr.enabled:
        raise error(503, "unavailable", "Requests aren't set up on the hub")


def _parse_media_id(media_id: str) -> tuple[str, int]:
    try:
        kind, tmdb = media_id.split(":", 1)
        if kind not in ("movie", "tv"):
            raise ValueError
        return kind, int(tmdb)
    except ValueError:
        raise error(400, "bad_params", "media_id looks like movie:123 or tv:456") from None


# -- routes --------------------------------------------------------------------
@router.get("/media/requests")
async def list_media_requests(scope: str = "mine", filter: str = "all", c: Caller = Depends(caller),
                              h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "media.request")
    _need_seerr(h)
    try:
        items = await build_list(h, c.subject.person_id)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if scope != "all" or _childlike(c):
        items = [i for i in items if i["mine"]]
    if filter == "active":
        items = [i for i in items if i["status"] in ACTIVE]
    elif filter == "ready":
        items = [i for i in items if i["status"] in READY]
    counts = {"active": sum(i["status"] in ACTIVE for i in items), "ready": sum(i["status"] in READY for i in items)}
    return {"items": items, "counts": counts}


@router.get("/media/requests/{req_id}")
async def get_media_request(req_id: int, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "media.request")
    _need_seerr(h)
    try:
        r = await h.jellyseerr.one_request(req_id)
        titles = await _titles(h, [r])
    except AdapterError as exc:
        raise error(404 if "404" in str(exc) else 503, "not_found", "That request is gone") from None
    m = r.get("media") or {}
    out = shape(r, titles.get(f"{m.get('mediaType') or r.get('type')}:{m.get('tmdbId')}", {}), Owners(h),
                c.subject.person_id)
    if _childlike(c) and not out["mine"]:
        raise error(404, "not_found", "That request is gone")
    out["can_cancel"] = out["status"] in ("waiting", "searching", "failed") and (out["mine"] or not _childlike(c))
    out["can_retry"] = out["status"] in ("failed", "searching") and not _childlike(c)
    return out


@router.get("/media/discover")
async def discover(q: str = "", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Search for anything to ask for (blank: what's popular), marked with what's already on Plex or asked for."""
    need(h, c, "media.request")
    _need_seerr(h)
    q = q.strip()[:100]
    try:
        rows = await (h.jellyseerr.search_raw(q) if q else h.jellyseerr.trending())
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    items = [_result(r) for r in rows if r.get("title") or r.get("name")]
    if _childlike(c):
        items = await _allowed_for_child(h, items)
    return {"items": items[:30], "query": q}


@router.get("/media/title/{media_id}")
async def title(media_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "media.request")
    _need_seerr(h)
    kind, tmdb = _parse_media_id(media_id)
    try:
        d = await h.jellyseerr.details(kind, tmdb, fresh=True)
    except AdapterError as exc:
        raise error(404 if "404" in str(exc) else 503, "not_found", "Couldn't find that title") from None
    rating = h.jellyseerr.content_rating(kind, d)
    if _childlike(c) and rating not in h.config.child_policy.ratings:
        raise error(404, "not_found", "Not available", ask_parent=True)
    mi = d.get("mediaInfo") or {}
    state, rid = _avail(mi)
    season_state = {s.get("seasonNumber"): s.get("status") for s in mi.get("seasons") or []}
    seasons = [{"n": s["seasonNumber"], "name": s.get("name"), "episodes": s.get("episodeCount"), "air_date": s.get("airDate"),
                "status": {5: "ready", 4: "partial", 3: "requested", 2: "requested"}.get(season_state.get(s["seasonNumber"]))}
               for s in d.get("seasons") or [] if s.get("seasonNumber")]
    runtime = d.get("runtime") or ((d.get("episodeRunTime") or [None])[0])
    return {"media_id": media_id, "kind": kind, "title": d.get("title") or d.get("name"), "year": _year(d),
            "poster_url": _poster(d), "summary": d.get("overview"), "rating": rating, "runtime_min": runtime,
            "genres": [g.get("name") for g in d.get("genres") or []][:3], "seasons": seasons,
            "availability": state, "request_id": rid, "plex_id": str(mi["ratingKey"]) if mi.get("ratingKey") else None}


class AskBody(BaseModel):
    media_id: str = Field(max_length=40)
    seasons: list[int] | None = Field(default=None, max_length=60)


@router.post("/media/requests")
async def ask(body: AskBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Ask for a title. It goes straight to Jellyseerr; nobody needs to approve it."""
    s = c.subject
    if not s.person_id:
        raise error(403, "pending", "Waiting for Dylan to say whose device this is")
    need(h, c, "media.request")
    _need_seerr(h)
    kind, tmdb = _parse_media_id(body.media_id)
    try:
        d = await h.jellyseerr.details(kind, tmdb, fresh=True)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if _childlike(c) and h.jellyseerr.content_rating(kind, d) not in h.config.child_policy.ratings:
        raise error(403, "forbidden", "That one needs a grown-up", ask_parent=True)
    state, rid = _avail(d.get("mediaInfo"))
    if state == "ready":
        raise error(409, "already_available", "That's already on Plex")
    name = d.get("title") or d.get("name") or "A title"
    if not h.actions.is_live("media.approve"):
        raise error(503, "dry_run", "Requests are switched off while the hub is in test mode")
    try:
        res = await h.jellyseerr.submit(kind, tmdb, body.seasons)
    except AdapterError as exc:
        h.db.log("media.request", "error", person_id=s.person_id, device_id=c.device["id"], target=body.media_id,
                 detail=str(exc)[:300])
        if rid and state in ("requested", "downloading", "partial"):
            raise error(409, "already_requested", "Someone already asked for that", request_id=rid) from None
        raise error(502, "failed", f"Jellyseerr didn't take it: {exc}") from None
    ext = str((res or {}).get("id") or "")
    h.db.insert("requests", {"id": new_id("req"), "created_at": time.time(), "kind": "media", "requester_id": s.person_id,
                             "title": name, "payload": {"media_id": body.media_id, "seasons": body.seasons},
                             "poster_url": _poster(d), "status": "approved", "decided_by": s.person_id,
                             "decided_at": time.time(), "external_id": ext or None})
    h.db.log("media.request", "ok", person_id=s.person_id, device_id=c.device["id"], target=body.media_id,
             params={"title": name, "seasons": body.seasons})
    if ext:
        try:
            r = await h.jellyseerr.one_request(int(ext))
            return {**shape(r, d, Owners(h), s.person_id), "can_cancel": True, "can_retry": False}
        except AdapterError:
            pass
    return {"id": ext, "media_id": body.media_id, "kind": kind, "title": name, "year": _year(d), "poster_url": _poster(d),
            "status": "searching", "status_text": STATUS_TEXT["searching"], "mine": True, "seasons": []}


async def _mine_or_adult(h: Hub, c: Caller, req_id: int) -> dict[str, Any]:
    try:
        r = await h.jellyseerr.one_request(req_id)
    except AdapterError:
        raise error(404, "not_found", "That request is gone") from None
    person = Owners(h).person(r)
    if _childlike(c) and not (person and person["id"] == c.subject.person_id):
        raise error(403, "forbidden", "Only the person who asked can do that", ask_parent=True)
    return r


@router.delete("/media/requests/{req_id}")
async def cancel(req_id: int, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "media.request")
    _need_seerr(h)
    r = await _mine_or_adult(h, c, req_id)
    if derive(r)["status"] in ("ready", "partial", "downloading"):
        raise error(409, "too_late", "It's already on its way. Remove it from Plex instead.")
    try:
        await h.jellyseerr.cancel(req_id)
    except AdapterError as exc:
        raise error(502, "failed", str(exc)) from None
    h.db.execute("UPDATE requests SET status='cancelled' WHERE kind='media' AND external_id=?", [str(req_id)])
    h.db.log("media.request.cancel", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=str(req_id))
    return {"ok": True}


@router.post("/media/requests/{req_id}/retry")
async def retry(req_id: int, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "media.approve")
    _need_seerr(h)
    try:
        await h.jellyseerr.retry(req_id)
    except AdapterError as exc:
        raise error(502, "failed", str(exc)) from None
    h.db.log("media.request.retry", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=str(req_id))
    return {"ok": True}


# -- "ready to watch" push, for every request a person made, wherever they made it -----
NOTIFIED = "media.ready_notified"


async def push_ready(h: Hub) -> int:
    if not h.jellyseerr.enabled:
        return 0
    items = await build_list(h, None, take=50)
    seen = h.db.kv_get(NOTIFIED)
    pending_rows = {r["external_id"] for r in h.db.all(
        "SELECT external_id FROM requests WHERE kind='media' AND status IN ('approved','downloading') "
        "AND external_id IS NOT NULL")}
    ready = [i for i in items if i["status"] == "ready"]
    if seen is None:  # first look: don't announce old news, only what the app is still waiting on
        seen = [i["id"] for i in ready if i["id"] not in pending_rows]
    seen_set = set(seen)
    sent = 0
    for i in ready:
        if i["id"] in seen_set:
            continue
        seen_set.add(i["id"])
        h.db.execute("UPDATE requests SET status='ready' WHERE kind='media' AND external_id=?", [i["id"]])
        person = h.db.person(i["person_id"]) if i["person_id"] else None
        if not person:
            continue
        ref = f"thomascreek://media/requests/{i['id']}"
        h.alerts.record("media", f"{i['title']} is ready to watch", "On Plex now", [person], ref=ref,
                        payload={"request_id": i["id"]})
        await h.notify.send(person_ids=[person["id"]], level="fyi", cat="media",
                            title=f"{i['title']} is ready to watch", body=None, ref=ref)
        sent += 1
    for i in items:  # keep the app's own rows in step for the older Requests list
        if i["status"] == "downloading":
            h.db.execute("UPDATE requests SET status='downloading' WHERE kind='media' AND external_id=? "
                         "AND status='approved'", [i["id"]])
    h.db.kv_set(NOTIFIED, sorted(seen_set, key=lambda x: int(x) if x.isdigit() else 0)[-500:])
    return sent
