"""TVs and boxes for the remote: find Android / Google TVs and Apple TVs and add them once, for everyone.

GET    /v1/remotes                     added devices + devices found on the network (?fresh=1 rescans)
POST   /v1/remotes/pair                {id, name?} start adding a found device -> a step
POST   /v1/remotes/pair/{flow_id}      {pin?} answer a step (the code on the TV, or Continue) -> next step
DELETE /v1/remotes/pair/{flow_id}      stop adding

Adults and admins only ("remotes.setup"). Pairing runs through Home Assistant, so the keys stay there and
every phone in the house can use the device as soon as it's added.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from ..remotes import SetupError
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1/remotes", tags=["remotes"])


def _ready(h: Hub, c: Caller) -> None:
    need(h, c, "remotes.setup")
    if not h.ha.enabled:
        raise error(409, "not_set_up", "Home Assistant isn't set up on the hub")


def _err(exc: SetupError):
    return error(exc.status, exc.code, exc.message)


@router.get("")
async def overview(fresh: bool = Query(default=False), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    try:
        return await h.remotes.overview(fresh=fresh)
    except SetupError as exc:
        raise _err(exc) from None


@router.post("/pair")
async def pair(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    item = str(body.get("id") or "")[:120]
    name = str(body.get("name") or "")[:80] or None
    try:
        out = await h.remotes.start(item, name)
    except SetupError as exc:
        raise _err(exc) from None
    _log(h, c, "remotes.pair_start", out, item)
    return out


@router.post("/pair/{flow_id}")
async def answer(flow_id: str, body: dict[str, Any] = Body(default={}), c: Caller = Depends(caller),
                 h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    pin = body.get("pin")
    try:
        out = await h.remotes.answer(flow_id, str(pin) if pin is not None else None)
    except SetupError as exc:
        raise _err(exc) from None
    if out["state"] in ("done", "failed"):
        _log(h, c, "remotes.add", out, out.get("entry_id") or flow_id)
    return out


@router.delete("/pair/{flow_id}")
async def cancel(flow_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    try:
        await h.remotes.cancel(flow_id)
    except SetupError as exc:
        raise _err(exc) from None
    return {"ok": True}


def _log(h: Hub, c: Caller, action: str, out: dict[str, Any], target: str) -> None:
    outcome = "ok" if out.get("state") in ("done", "form") else "failed"
    h.db.log(action, outcome, person_id=c.subject.person_id, device_id=c.device["id"], target=target,
             params={"kind": out.get("kind"), "name": out.get("name"), "step": out.get("step")},
             detail=out.get("reason"))
