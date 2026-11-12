"""Snapshot, live events and actions."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..actions import ActionError
from ..events import sse
from ..state import Hub
from .deps import Caller, caller, error, hub, require_confirm

router = APIRouter(prefix="/v1", tags=["home"])


@router.get("/home")
async def home(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.home.snapshot(c.subject)


@router.get("/events")
async def events(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> StreamingResponse:
    s = c.subject

    def accept(ev: dict[str, Any]) -> dict[str, Any] | None:
        if ev["type"] == "device":
            d = h.home.device(ev["entity_id"], s)
            return {"type": "device", "data": d} if d else None
        if ev["type"] == "status":
            return {"type": "status", "data": h.home.status(s, [])}
        return ev

    sub = h.bus.subscribe(accept, s.person_id)

    async def stream():
        try:
            yield sse({"type": "hello", "data": {"role": s.role}})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=20)
                    yield sse(ev)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            h.bus.unsubscribe(sub)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ActionBody(BaseModel):
    action: str = Field(max_length=60)
    target: str | None = Field(default=None, max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/actions")
async def actions(body: ActionBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if body.action.startswith("_") or any(k.startswith("_") for k in body.params):
        raise error(400, "bad_params", "Unknown parameter")
    if body.action == "media.play":
        raise error(400, "use_media_play", "Use POST /v1/media/play")
    return await perform(request, c, h, body.action, body.target, body.params)


async def perform(request: Request, c: Caller, h: Hub, action: str, target: str | None,
                  params: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = h.actions.plan(c.subject, action, target, params)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message, **exc.extra) from None
    d = plan.decision
    if not d.allowed:
        h.db.log(action, "denied", person_id=c.subject.person_id, device_id=c.device["id"], target=target,
                 params={k: v for k, v in params.items() if not k.startswith("_")}, detail=d.reason)
        raise error(403, "forbidden", "Ask a parent" if d.ask_parent else "Not allowed", ask_parent=d.ask_parent)
    await require_confirm(request, h, c, d.tier)
    try:
        via = "ask" if request.headers.get("x-tc-via") == "ask" else "app"
        return await h.actions.run(c.subject, plan, source=via)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message) from None


def dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))
