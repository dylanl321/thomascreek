"""Ask for a canonical title and open the stream the hub chose."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..iptvcanon import copies
from ..iptvplay import accounts, fail_checked, target, tune
from ..policy import Target
from ..tvapp import TvAppError
from .deps import Caller, caller, caller_ip, error, hub, need
from .tvguide import _media

router = APIRouter(prefix="/v1/tv", tags=["tv"])


@router.get("/accounts")
async def tv_accounts(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return accounts(h)


@router.get("/copies")
async def tv_copies(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    """Titles the provider lists more than once. A TV shows one row for each and plays it through /tune."""
    _media(h, c)
    return copies(h.db)


@router.post("/tune")
async def tv_tune(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    kids = _media(h, c)
    canonical_id = str((body or {}).get("canonical_id") or (body or {}).get("episode_id") or "")
    if not canonical_id:
        return {"ok": False, "message": "That title isn't in the catalog."}
    if canonical_id.startswith("ep:") and kids:
        episode = h.tv_catalog.episode(canonical_id) if h.tv_catalog else None
        if not episode or not episode.get("kids"):
            return {"ok": False, "message": "That show isn't in the kids' library."}
    if c.subject.tv and c.subject.tv_id:
        tv_id = c.subject.tv_id
    else:
        device = str((body or {}).get("device") or "")
        if not device:
            return {"ok": False, "message": "Say which room."}
        try:
            tv_id, tv = h.tv_app.resolve(device)
        except TvAppError:
            return {"ok": False, "message": "That room isn't one you can use."}
        where = Target(entity_id=tv.get("media_entity") or tv_id, area=tv.get("room"), kind="tv")
        if not h.policy.decide(c.subject, "tv.play", where, {}).allowed:
            return {"ok": False, "message": "That room isn't one you can use."}
    return tune(h, canonical_id, tv_id)


@router.post("/tune/fail")
async def tv_tune_fail(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    _media(h, c)
    if not c.subject.tv or not c.subject.tv_id:
        return {"ok": False, "message": "That stream has ended."}
    # A blocked provider address moves the stream to the provider's next address before another login.
    return await fail_checked(h, str((body or {}).get("session") or ""), c.subject.tv_id)


@router.get("/stream/{session}")
async def tv_stream(session: str, request: Request, c: Caller = Depends(caller), h=Depends(hub)) -> RedirectResponse:
    if not h.is_home(caller_ip(request, h)) or not c.subject.tv or not c.subject.tv_id:
        raise error(404, "not_found", "That stream has ended.")
    url = target(h, session, c.subject.tv_id)
    if not url:
        raise error(404, "not_found", "That stream has ended.")
    return RedirectResponse(url, status_code=302)
