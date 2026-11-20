"""The folded catalog: one title, every provider copy."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from ..iptvcanon import merge, search, split, unsure
from .deps import Caller, caller, error, hub, need
from .tvguide import _media

router = APIRouter(prefix="/v1/tv/catalog", tags=["tv"])


@router.get("")
async def catalog(q: str = "", kind: str | None = None, c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    _media(h, c)
    if kind not in (None, "", "movie", "series", "live"):
        raise error(400, "bad_request", "kind is movie, series, or live")
    return search(h.db, q, kind or None)


@router.get("/unsure")
async def catalog_unsure(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return await asyncio.to_thread(unsure, h.db)


@router.post("/merge")
async def catalog_merge(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) < 2:
        raise error(400, "bad_request", "Name the two titles to fold together.")
    result = await asyncio.to_thread(merge, h.db, [str(item) for item in ids])
    if not result:
        raise error(404, "not_found", "That title isn't in the catalog.")
    return result


@router.post("/split")
async def catalog_split(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    item_id = str(body.get("id") or "")
    provider = str(body.get("provider") or "")
    stream_id = str(body.get("stream_id") or "")
    if not item_id or not provider or not stream_id:
        raise error(400, "bad_request", "Name the title and the copy to separate.")
    result = await asyncio.to_thread(split, h.db, item_id, provider, stream_id)
    if not result:
        raise error(404, "not_found", "That title isn't in the catalog.")
    return result
