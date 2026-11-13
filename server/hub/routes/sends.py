"""Grown-ups send films, shows and YouTube to the kids' iPads (see hub/sends.py).

Grown-ups: ``GET /v1/sends`` (each kid, their devices and what's on them), ``GET /v1/sends/choices``
(the options for one title: seasons, episode counts, sizes), ``POST /v1/sends``, ``PATCH`` and
``DELETE /v1/sends/{id}``, ``POST /v1/sends/sync`` (top up now).

Every device: ``GET /v1/sends/mine`` (what it should hold) and ``POST /v1/sends/inventory`` (what it holds).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..home import iso
from ..sends import SHOW_SCOPES
from ..state import Hub
from ..youtube import YouTubeError, parse_link
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/sends", tags=["sends"])

PARENTS = ("admin", "adult")


def _parent(c: Caller) -> None:
    if c.subject.role not in PARENTS:
        raise error(403, "forbidden", "Ask a grown-up", ask_parent=True)


def _targets(h: Hub) -> list[dict[str, Any]]:
    """Who can be sent to: kids first, then anyone else who already has sends."""
    kids = h.db.people_with_role("child")
    others = [h.db.person(pid) for pid in h.sends.people_with_sends()]
    seen = {p["id"] for p in kids}
    return kids + [p for p in others if p and p["id"] not in seen]


def _first(name: str | None) -> str | None:
    return name.split()[0] if name else None


# -- what's on each kid's devices ------------------------------------------------
@router.get("")
async def overview(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _parent(c)
    out = []
    for p in _targets(h):
        items, status = await h.sends.lineup(p["id"])
        by_id = {i["item_id"]: i for i in items}
        reports = h.sends.device_reports(p["id"])
        devices = []
        for d in h.db.devices(p["id"]):
            if d["status"] != "active" or d.get("kind") == "test":
                continue
            r = reports.get(d["id"]) or {}
            holdings = r.get("holdings") or []
            sent = [x for x in holdings if x.get("item_id") in by_id or x.get("sent")]
            devices.append({
                "id": d["id"], "name": d["name"], "model": d.get("model"),
                "reported_at": iso(r["reported_at"]) if r.get("reported_at") else None,
                "free_bytes": r.get("free_bytes"), "total_bytes": r.get("total_bytes"),
                "used_bytes": sum(x.get("bytes") or 0 for x in holdings if x.get("state") == "ready"),
                "sent_bytes": sum(x.get("bytes") or 0 for x in sent if x.get("state") == "ready"),
                "holdings": holdings,
                "can_wake": bool(d.get("apns_token")),
            })
        held = {d["id"]: {x["item_id"]: x for x in d["holdings"] if x.get("item_id")} for d in devices}
        sends = []
        for s in h.sends.all(p["id"]):
            st = status.get(s["id"], {})
            rows = []
            for iid in st.get("item_ids", []):
                i = by_id.get(iid) or {"item_id": iid}
                rows.append({**i, "on": {dev: (held[dev].get(iid) or {}).get("state", "missing") for dev in held}})
            who = h.db.person(s.get("created_by"))
            sends.append({
                "id": s["id"], "kind": s["kind"], "ref": s["ref"], "title": s["title"], "poster_url": s.get("poster_url"),
                "scope": s["scope"], "count": s.get("count"), "season": s.get("season"), "quality": s["quality"],
                "from": _first(who["name"]) if who else None, "created_at": iso(s["created_at"]),
                "state": st.get("state", "waiting"), "detail": st.get("detail"), "error": s.get("last_error"),
                "items": rows,
            })
        out.append({"person": {"id": p["id"], "name": p["name"], "role": p["role"], "ui_style": p.get("ui_style")},
                    "devices": devices, "sends": sends})
    return {"people": out}


# -- choosing what to send ----------------------------------------------------------
@router.get("/choices")
async def choices(kind: str, ref: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """What can be picked for one title: for a show, its seasons and an episode size; for YouTube, the channel."""
    _parent(c)
    try:
        if kind == "show":
            eps = await h.sends._show_episodes(ref)
            seasons: dict[int, int] = {}
            for e in eps:
                seasons[e.get("season") or 0] = seasons.get(e.get("season") or 0, 0) + 1
            per_ep = await _bytes_each(h, eps[-1]["id"]) if eps else {}
            return {"kind": "show", "episodes": len(eps),
                    "seasons": [{"season": k, "episodes": v} for k, v in sorted(seasons.items())],
                    "bytes_per_episode": per_ep}
        if kind == "title":
            if ref.startswith("yt-"):
                v = h.youtube.video(ref[3:])
                return {"kind": "title", "bytes_per_episode": {"original": (v or {}).get("bytes")}}
            return {"kind": "title", "bytes_per_episode": await _bytes_each(h, ref)}
        if kind == "youtube":
            link = parse_link(ref)
            if link["kind"] == "video":
                raise error(400, "bad_link", "That's one video. Send it as a single video.")
            info = await h.youtube.lookup(link, 20)
            sizes = [v.get("duration_s") or 0 for v in info["videos"]]
            avg_s = sum(sizes) / len(sizes) if sizes else 600
            # 1080p H.264 from YouTube runs about 3 Mbps.
            return {"kind": "youtube", "title": info["title"], "channel": info["channel"], "picture": info["thumb"],
                    "videos": len(info["videos"]), "bytes_per_episode": {"original": int(avg_s * 3_000_000 / 8)}}
    except YouTubeError as exc:
        raise error(400, "bad_link", str(exc)) from None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    raise error(400, "bad_params", "kind is show, title or youtube")


async def _bytes_each(h: Hub, item_id: str) -> dict[str, int | None]:
    from ..downloads import options

    media = await h.plex.media_info(item_id)
    return {o["quality"]: o["bytes"] for o in options(media)} if media else {}


class SendBody(BaseModel):
    person_ids: list[str] = Field(min_length=1, max_length=8)
    kind: str = Field(pattern="^(title|show|youtube|request)$")
    ref: str = Field(min_length=1, max_length=400)
    title: str | None = Field(default=None, max_length=200)
    poster_url: str | None = Field(default=None, max_length=400)
    scope: str | None = Field(default=None, pattern="^(one|next|last|season|all|newest)$")
    count: int | None = Field(default=None, ge=1, le=100)
    season: int | None = Field(default=None, ge=0, le=200)
    quality: str = Field(default="high", pattern="^(original|high|small)$")


@router.post("")
async def create(body: SendBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _parent(c)
    people = [h.db.person(pid) for pid in body.person_ids]
    if any(p is None for p in people):
        raise error(404, "not_found", "Pick someone in the house")
    kind, ref, title, poster = body.kind, body.ref, body.title, body.poster_url
    scope, count = body.scope or "one", body.count
    try:
        if kind == "title":
            if ref.startswith("yt-"):
                v = h.youtube.video(ref[3:])
                if v is None:
                    raise error(404, "not_found", "That video isn't on the hub")
                title, poster = v["title"], h.youtube.item(v)["poster_url"]
            else:
                item = await h.plex.metadata(ref)
                if item is None or item["kind"] not in ("movie", "episode", "show"):
                    raise error(404, "not_found", "That isn't in the library")
                if item["kind"] == "show":
                    kind = "show"
                title, poster = title or item["title"], poster or item.get("poster_url")
            scope = "one" if kind == "title" else scope
        if kind == "show":
            if scope not in SHOW_SCOPES:
                scope = "next"
            if scope in ("next", "last"):
                count = count or 10
            if not title:
                item = await h.plex.metadata(ref)
                if item is None:
                    raise error(404, "not_found", "That show isn't in the library")
                title, poster = item["title"], poster or item.get("poster_url")
        if kind == "youtube":
            count = count or 10
            scope = "newest"
            if not ref.startswith("yts_"):
                if parse_link(ref)["kind"] == "video":
                    raise error(400, "bad_link", "That's one video. Send it as a single video.")
                src = await h.youtube.add(ref, True, c.subject.person_id or "", latest=count)
                ref = src["id"]
            src_row = h.db.one("SELECT * FROM yt_sources WHERE id=?", [ref])
            if src_row is None:
                raise error(404, "not_found", "That channel isn't on the hub")
            # Keep checking it, and look far enough back for the number asked for.
            vals: dict[str, Any] = {"follow": 1} if src_row["kind"] in ("channel", "playlist") else {}
            if count > (src_row.get("latest") or 0):
                vals["latest"] = count
            h.db.update("yt_sources", "id", ref, vals)
            if vals.get("latest"):
                h.db.update("yt_sources", "id", ref, {"last_checked_at": 0})  # look again now
                h.youtube._wake.set()
            title, poster = title or src_row["title"], poster or f"/v1/youtube/picture/src-{ref}"
        if kind == "request":
            kind_, _, tmdb = ref.partition(":")
            if kind_ not in ("movie", "tv") or not tmdb.isdigit():
                raise error(400, "bad_params", "ref looks like movie:123 or tv:456")
            if not title:
                raise error(400, "bad_params", "title is required")
            scope = scope if kind_ == "tv" and scope in SHOW_SCOPES else ("next" if kind_ == "tv" else "one")
            if scope in ("next", "last"):
                count = count or 10
    except YouTubeError as exc:
        raise error(400, "bad_link", str(exc)) from None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    made = []
    for p in people:
        s = h.sends.add(p["id"], kind, ref, title or "Untitled", poster_url=poster, scope=scope, count=count,
                        season=body.season if kind in ("show", "request") and scope == "season" else None,
                        quality=body.quality, created_by=c.subject.person_id)
        made.append(s["id"])
        h.db.log("sends.add", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=p["id"],
                 params={"title": title, "kind": kind, "scope": scope, "count": count})
    return {"ids": made, "title": title}


class UpdateBody(BaseModel):
    scope: str | None = Field(default=None, pattern="^(one|next|last|season|all|newest)$")
    count: int | None = Field(default=None, ge=1, le=100)
    season: int | None = Field(default=None, ge=0, le=200)
    quality: str | None = Field(default=None, pattern="^(original|high|small)$")


@router.patch("/{send_id}")
async def update(send_id: str, body: UpdateBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _parent(c)
    s = h.sends.get(send_id)
    if s is None:
        raise error(404, "not_found", "That send is gone")
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if s["kind"] in ("title", "youtube"):
        vals.pop("scope", None)
        vals.pop("season", None)
    if s["kind"] == "youtube" and vals.get("count"):
        src = h.db.one("SELECT latest FROM yt_sources WHERE id=?", [s["ref"]])
        if src is not None and vals["count"] > (src.get("latest") or 0):
            h.db.update("yt_sources", "id", s["ref"], {"latest": vals["count"], "last_checked_at": 0})
            h.youtube._wake.set()
    h.sends.update(send_id, vals)
    return {"ok": True}


@router.delete("/{send_id}")
async def remove(send_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Stop sending it; the iPads delete what it brought the next time they check."""
    _parent(c)
    s = h.sends.get(send_id)
    if s is None or not h.sends.remove(send_id):
        raise error(404, "not_found", "That send is gone")
    h.db.log("sends.remove", "ok", person_id=c.subject.person_id, target=s["person_id"], params={"title": s["title"]})
    return {"ok": True}


class SyncBody(BaseModel):
    person_id: str | None = None


@router.post("/sync")
async def sync(body: SyncBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Top up now: work everything out again, get files ready and wake the iPads."""
    _parent(c)
    pids = [body.person_id] if body.person_id else h.sends.people_with_sends()
    out = {}
    for pid in pids:
        try:
            out[pid] = await h.sends.sync(pid, force_push=True)
        except AdapterError as exc:
            raise error(503, "unavailable", str(exc)) from None
    return {"people": out}


# -- the device's side --------------------------------------------------------------
@router.get("/mine")
async def mine(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """What this person's devices should hold. Sent titles not listed here can be deleted."""
    pid = c.subject.person_id
    if not pid or not h.sends.all(pid):
        return {"items": [], "active": False}
    try:
        items, _ = await h.sends.lineup(pid)
    except AdapterError:
        # Plex didn't answer: say nothing changed rather than an empty list (which would delete things).
        raise error(503, "unavailable", "Couldn't check what to download") from None
    return {"items": items, "active": True, "checked_at": iso(time.time())}


class Holding(BaseModel):
    item_id: str = Field(max_length=80)
    title: str | None = Field(default=None, max_length=200)
    quality: str | None = Field(default=None, max_length=20)
    state: str = Field(max_length=20)  # preparing | downloading | ready | failed
    progress: float | None = None
    bytes: int | None = None
    watched: bool = False
    sent: bool = False
    error: str | None = Field(default=None, max_length=200)


class InventoryBody(BaseModel):
    items: list[Holding] = Field(default_factory=list, max_length=1000)
    free_bytes: int | None = None
    total_bytes: int | None = None


@router.post("/inventory")
async def inventory(body: InventoryBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    h.sends.report(c.device, [i.model_dump() for i in body.items], body.free_bytes, body.total_bytes)
    return {"ok": True}
