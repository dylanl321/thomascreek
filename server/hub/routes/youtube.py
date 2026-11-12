"""Kids' YouTube: add a link, list what's saved, parents remove or block (see hub/youtube.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..state import Hub
from ..youtube import YouTubeError, parse_link
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/youtube", tags=["youtube"])

ADDERS = ("admin", "adult", "child")
PARENTS = ("admin", "adult")


def _can_watch(h: Hub, c: Caller) -> None:
    if not h.youtube.enabled:
        raise error(404, "not_found", "YouTube is turned off on the hub")
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)


def _parent(c: Caller) -> None:
    if c.subject.role not in PARENTS:
        raise error(403, "forbidden", "Ask a grown-up", ask_parent=True)


@router.get("")
async def overview(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Everything the family added: sources, then videos newest first."""
    _can_watch(h, c)
    yt = h.youtube
    sources = [yt.source_out(s) for s in h.db.all("SELECT * FROM yt_sources ORDER BY created_at DESC")]
    rows = h.db.all("SELECT * FROM yt_videos WHERE status IN ('ready','queued','fetching','failed') "
                    "ORDER BY COALESCE(ready_at, added_at) DESC LIMIT 500")
    out: dict[str, Any] = {"sources": sources, "videos": [yt.video_out(v) for v in rows]}
    if c.subject.role in PARENTS:
        out["storage"] = {k: v for k, v in yt.summary().items() if k in ("bytes", "cap_bytes", "ready", "waiting", "failed", "yt_dlp")}
        out["blocked"] = h.db.all("SELECT channel_id, name, blocked_at FROM yt_blocked ORDER BY blocked_at DESC")
    return out


@router.get("/preview")
async def preview(url: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """What a link would bring, before adding it."""
    _can_watch(h, c)
    try:
        link = parse_link(url)
        info = await h.youtube.lookup(link)
    except YouTubeError as exc:
        raise error(400, "bad_link", str(exc)) from None
    if h.youtube.blocked(info["channel_id"]):
        raise error(403, "blocked", "A grown-up turned that channel off.")
    vids = info["videos"]
    return {"kind": info["kind"], "title": info["title"], "channel": info["channel"], "picture": info["thumb"],
            "followers": info.get("followers"), "count": len(vids), "newest": vids[0]["title"] if vids else None,
            "followable": info["kind"] != "video"}


class AddBody(BaseModel):
    url: str = Field(min_length=5, max_length=500)
    follow: bool = True


@router.post("")
async def add(body: AddBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    s = c.subject
    if s.role not in ADDERS or not s.person_id:
        raise error(403, "forbidden", "Not available")
    try:
        src = await h.youtube.add(body.url, body.follow, s.person_id)
    except YouTubeError as exc:
        raise error(400, "bad_link", str(exc)) from None
    h.db.log("youtube.add", "ok", person_id=s.person_id, device_id=c.device["id"], target=src["id"],
             params={"title": src["title"], "kind": src["kind"], "url": src["url"]})
    # No approval step (Dylan, 2026-11-12): parents just hear about it.
    who = (h.db.person(s.person_id) or {}).get("name", "Someone")
    kind = {"channel": "channel", "playlist": "playlist"}.get(src["kind"], "video")
    await h.notify.send(roles=PARENTS, level="fyi", cat="media", title=f"{who} added {src['title']}",
                        body=f"YouTube {kind}", ref="thomascreek://youtube", exclude_person=s.person_id)
    return src


class FollowBody(BaseModel):
    follow: bool


@router.post("/sources/{source_id}")
async def set_follow(source_id: str, body: FollowBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    src = h.db.one("SELECT * FROM yt_sources WHERE id=?", [source_id])
    if src is None:
        raise error(404, "not_found", "Not found")
    if c.subject.role not in PARENTS and src["added_by"] != c.subject.person_id:
        raise error(403, "forbidden", "Ask a grown-up", ask_parent=True)
    h.db.update("yt_sources", "id", source_id, {"follow": int(body.follow and src["kind"] != "video")})
    return h.youtube.source_out(h.db.one("SELECT * FROM yt_sources WHERE id=?", [source_id]))  # type: ignore[arg-type]


@router.delete("/sources/{source_id}")
async def remove_source(source_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    src = h.db.one("SELECT * FROM yt_sources WHERE id=?", [source_id])
    if src is None:
        raise error(404, "not_found", "Not found")
    # Children may take back what they added themselves.
    if c.subject.role not in PARENTS and src["added_by"] != c.subject.person_id:
        raise error(403, "forbidden", "Ask a grown-up", ask_parent=True)
    h.youtube.remove_source(source_id)
    h.db.log("youtube.remove", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=source_id,
             params={"title": src["title"]})
    return {"ok": True}


@router.delete("/videos/{video_id}")
async def remove_video(video_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    _parent(c)
    v = h.youtube.video(video_id)
    if v is None:
        raise error(404, "not_found", "Not found")
    h.youtube.remove_video(video_id)
    h.db.log("youtube.remove_video", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=video_id,
             params={"title": v["title"]})
    return {"ok": True}


@router.post("/videos/{video_id}/retry")
async def retry(video_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    v = h.youtube.video(video_id)
    if v is None or v["status"] not in ("failed", "trimmed"):
        raise error(404, "not_found", "Nothing to try again")
    h.db.update("yt_videos", "id", video_id, {"status": "queued", "error": None})
    h.youtube._wake.set()
    return h.youtube.video_out(h.youtube.video(video_id))  # type: ignore[arg-type]


class BlockBody(BaseModel):
    channel_id: str = Field(min_length=3, max_length=64)
    name: str | None = Field(default=None, max_length=200)


@router.post("/block")
async def block(body: BlockBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    _parent(c)
    h.youtube.block(body.channel_id, body.name, c.subject.person_id)
    h.db.log("youtube.block", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=body.channel_id,
             params={"name": body.name})
    return {"ok": True}


@router.delete("/block/{channel_id}")
async def unblock(channel_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _can_watch(h, c)
    _parent(c)
    h.youtube.unblock(channel_id)
    return {"ok": True}


@router.post("/refresh")
async def refresh(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Parents: look for new videos now instead of waiting for the next check."""
    _can_watch(h, c)
    _parent(c)
    found = await h.youtube.refresh(force=True)
    h.youtube._wake.set()
    return {"new": found}


@router.get("/picture/{name}")
async def picture(name: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    p = h.youtube.picture(name)
    if p is None:
        raise error(404, "not_found", "No picture")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
