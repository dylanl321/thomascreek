"""Idle screen for a TV: a seasonal scene, and the widgets around it."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse, Response

from ..adapters.base import AdapterError
from ..ambient_scenes import CATEGORIES, SceneError
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/ambient", tags=["tv"])


def _see(h: Hub, c: Caller) -> None:
    if c.subject.role in ("pending", "guest"):
        raise error(403, "forbidden", "Not available")
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv") or c.subject.tv):
        raise error(403, "forbidden", "Not available")


@router.get("")
async def ambient(device: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The scene for today, plus weather, the day, a few cameras and whatever art is already cached."""
    _see(h, c)
    # The art walks the whole guide for what is on now: in a worker thread, so other calls are not held up.
    return await asyncio.to_thread(h.ambient.view, device)


@router.get("/library")
async def library(device: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The eight scenes, so the phone can pin one or point it at a channel."""
    _see(h, c)
    return h.ambient.library(device)


@router.put("")
async def update(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only Dylan can change this")
    return h.ambient.update(body)


PARENTS = ("admin", "adult")


def _parent(c: Caller) -> None:
    if c.subject.tv or c.subject.role not in PARENTS:
        raise error(403, "forbidden", "Ask a grown-up to change the scenes", ask_parent=True)


def _scene_error(exc: SceneError) -> Exception:
    return error(exc.status, exc.code, exc.message)


@router.get("/scenes")
async def scenes(device: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """YouTube ambiance scenes by category, the season's pick first, and this TV's idle settings.

    Each scene has `stream_url` when the hub saved a clip the TV can loop, and always `youtube_url`, which a TV
    opens in the YouTube app otherwise. A press on an unsaved scene should also POST `/scenes/{id}/save`.
    """
    _see(h, c)
    child = c.subject.is_child_like or c.subject.role == "child"
    return h.ambient.scenes.page(device, child, c.subject.role in PARENTS and not c.subject.tv, h.ambient._now(None))


@router.get("/categories")
async def categories(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _see(h, c)
    return {"categories": [{"id": row["id"], "title": row["title"]} for row in CATEGORIES]}


@router.get("/search")
async def search(q: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Find a scene on YouTube to add (parents, on the phone). A pasted link finds that one video."""
    _see(h, c)
    _parent(c)
    if not h.youtube.enabled:
        raise error(404, "not_found", "YouTube is turned off on the hub")
    return {"q": q[:100], "items": await h.ambient.scenes.search(q)}


@router.post("/scenes")
async def add_scene(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Put a YouTube video in a category: {"video": link or id, "category": id, "title"?}. Its clip is saved."""
    _see(h, c)
    _parent(c)
    try:
        item = await h.ambient.scenes.add(str(body.get("video") or ""), str(body.get("category") or ""),
                                          c.subject.person_id, body.get("title") if isinstance(body.get("title"), str) else None)
    except SceneError as exc:
        raise _scene_error(exc) from None
    h.db.log("ambient.add", "ok", person_id=c.subject.person_id, target=item.get("id"), source="app")
    return item


@router.delete("/scenes/{video_id}")
async def remove_scene(video_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Take a scene out of the list, for good (a suggestion doesn't come back)."""
    _see(h, c)
    _parent(c)
    try:
        h.ambient.scenes.remove(video_id)
    except SceneError as exc:
        raise _scene_error(exc) from None
    h.db.log("ambient.remove", "ok", person_id=c.subject.person_id, target=video_id, source="app")
    return {"ok": True}


@router.post("/scenes/{video_id}/save")
async def save_scene(video_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Someone pressed a scene that isn't saved yet: save its clip so it loops here next time."""
    _see(h, c)
    item = h.ambient.scenes.item(video_id)
    if item is None:
        raise error(404, "gone", "That scene isn't in the list any more.")
    state = h.ambient.scenes.want(video_id)
    messages = {
        "ready": "It's ready on the hub.",
        "queued": "The hub is saving it; it will play here in a few minutes.",
        "saving": "The hub is saving it now.",
        "failed": "YouTube didn't give it to the hub, so it opens in the YouTube app.",
        "online": "It opens in the YouTube app.",
    }
    return {"state": state, "message": messages.get(state, "")}


@router.put("/idle")
async def set_idle(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Idle screen: {"enabled"?, "after_min"?, "sound"?, "choice"?: "season"|"shuffle"|video id, "device"?, "reset"?}."""
    _see(h, c)
    _parent(c)
    try:
        return h.ambient.scenes.set_idle(body if isinstance(body, dict) else {})
    except SceneError as exc:
        raise _scene_error(exc) from None


@router.get("/videos/{name}")
async def clip(name: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    """A saved scene clip. Range requests work, so both TVs can seek and loop."""
    _see(h, c)
    vid = name.removesuffix(".mp4")
    path = h.ambient.scenes.file(vid)
    if path is None:
        raise error(404, "gone", "That scene isn't saved on the hub yet; open it in YouTube.")
    h.ambient.scenes.played(vid)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/pictures/{name}")
async def picture(name: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    _see(h, c)
    path = h.ambient.scenes.picture(name.removesuffix(".jpg"))
    if path is None:
        raise error(404, "gone", "No picture")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@router.get("/cameras/{name}.jpg")
async def camera(name: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """A camera still for the idle screen. The TV uses its own sign-in."""
    _see(h, c)
    if not name.replace("_", "").isalnum():
        raise error(404, "gone", "That camera isn't on the screen")
    try:
        if h.frigate.enabled:
            data = await h.frigate.latest_jpg(name)
        else:
            data = await h.ha.camera_proxy(f"camera.{name}")
    except AdapterError:
        raise error(404, "gone", "That camera didn't answer") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})
