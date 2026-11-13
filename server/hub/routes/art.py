"""Cached artwork for the TV app and the phone.

GET /v1/art/{kind}/{key}?size=   logo, poster, backdrop or still. ETag is the sha256 of the file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from ..artwork import ArtError
from ..state import Hub
from .deps import Caller, caller, error, hub
from .tvguide import _media

router = APIRouter(prefix="/v1", tags=["tv"])

CACHE = "private, max-age=604800"


@router.get("/art/{kind}/{key:path}")
async def art(kind: str, key: str, request: Request, size: str = "m",
              c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """A cached image. `size` is s, m, l, original, or a TMDB width such as w342."""
    _media(h, c)
    try:
        path, etag, media = await h.artwork.file(kind, key, size)
    except ArtError as exc:
        raise error(exc.status, exc.code, exc.message) from None
    headers = {"ETag": etag, "Cache-Control": CACHE}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media, headers=headers)
