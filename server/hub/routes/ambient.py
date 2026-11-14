"""Idle screen for a TV: a seasonal scene, and the widgets around it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..adapters.base import AdapterError
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
    return h.ambient.view(device)


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
