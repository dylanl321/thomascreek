"""Universal title search and the request that follows it.

GET  /v1/titles/search?q=     one card per title, across Plex, IPTV and TMDB
POST /v1/titles/request       {media_id} into Jellyseerr, or a parent approval for a child
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..state import Hub
from ..titles import TitleError
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/titles", tags=["titles"])


def _err(exc: TitleError):
    return error(exc.status, exc.code, exc.message, **exc.extra)


def _can_browse(h: Hub, c: Caller) -> None:
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")
            or h.policy.has(c.subject, "media.request")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)


@router.get("/search")
async def title_search(q: str = "", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Films and shows from Plex, the TV providers and TMDB, one card each."""
    _can_browse(h, c)
    try:
        return await h_search(h, c, q)
    except TitleError as exc:
        raise _err(exc) from None


async def h_search(h: Hub, c: Caller, q: str) -> dict[str, Any]:
    from ..titles import search

    return await search(h, c.subject, q)


class RequestIn(BaseModel):
    media_id: str = Field(max_length=40)


@router.post("/request")
async def title_request(body: RequestIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Ask Jellyseerr for a title. A child's ask waits for a parent."""
    from ..titles import request_title

    try:
        return await request_title(h, c.subject, body.media_id, c.device.get("id"))
    except TitleError as exc:
        raise _err(exc) from None
