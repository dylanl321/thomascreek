"""Phone-facing TV guide, series, movies and the list of TVs a person can play on.

Reads use the same bearer as the rest of the app. A child, a guest or a kids' TV
only receives titles in the kids' categories. Nothing here includes the provider
login or a stream address.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..actions import ActionError
from ..adapters.base import AdapterError
from ..policy import Target
from ..state import Hub
from ..tvapp import TvAppError
from .. import tvcontrol, tvexplore, tvhandoff
from ..upnext import shelf
from .deps import Caller, caller, error, hub, need
from .media import _filter, _watch_cap

router = APIRouter(prefix="/v1/tv", tags=["tv"])


def _media(h: Hub, c: Caller) -> bool:
    """True when this caller only sees kids' titles."""
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)
    return c.subject.is_child_like


async def _ready(h: Hub) -> None:
    try:
        await h.tv_catalog.ensure()
    except TvAppError as exc:
        raise error(exc.status, exc.code, exc.message) from None


@router.get("/explore/art/{kind}/{media}/{tmdb_id}")
async def explore_art(kind: str, media: str, tmdb_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """A cached poster, backdrop, logo or title card. A missing file is a quiet 404."""
    _media(h, c)
    found = await tvexplore.cached_art(h, kind, media, tmdb_id)
    if found is None:
        raise error(404, "not_found", "No artwork")
    body, mime = found
    return Response(body, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})
