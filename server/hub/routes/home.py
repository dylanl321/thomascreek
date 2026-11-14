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
from ..policy import Subject
from ..state import Hub
from .deps import Caller, caller, error, hub, require_confirm

# What a TV subscription is allowed to receive. Anything else (cameras, printers,
# other people's now-playing) is dropped. Doorbell and house alerts arrive as inbox.
_TV_EVENTS = frozenset({"device", "status", "inbox", "tv_command", "snapshot", "home_alert"})
_TV_ALERTS = frozenset({"house", "kitchen", "weather", "doors"})

router = APIRouter(prefix="/v1", tags=["home"])


@router.get("/home")
async def home(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.home.snapshot(c.subject)


def shape_event(h: Hub, s: Subject, ev: dict[str, Any]) -> dict[str, Any] | None:
    """What this caller may see of one bus event. None drops it."""
    if s.tv:
        return _shape_for_tv(h, s, ev)
    if ev.get("type") == "tv_command":
        return None
    if ev.get("type") == "home_alert":
        if s.role in ("pending", "guest"):
            return None
        return ev
    if ev.get("type") == "tv":
        return _shape_tv_watching(h, s, ev)
    if ev.get("type") == "device":
        device = h.home.device(ev["entity_id"], s)
        return {"type": "device", "data": device} if device else None
    if ev.get("type") == "status":
        return {"type": "status", "data": h.home.status(s, [])}
    return ev


def _shape_for_tv(h: Hub, s: Subject, ev: dict[str, Any]) -> dict[str, Any] | None:
    kind = ev.get("type")
    if kind not in _TV_EVENTS:
        return None
    if kind == "device":
        device = h.home.device(ev["entity_id"], s)
        return {"type": "device", "data": device} if device else None
    if kind == "status":
        return {"type": "status", "data": h.home.status(s, [])}
    if kind == "snapshot":
        return {"type": "snapshot", "data": ev.get("data") or {}}
    if kind == "tv_command":
        data = ev.get("data") or {}
        if data.get("install_id") not in (None, s.tv_id):
            return None
        return {"type": "tv_command", "data": data}
    if kind == "home_alert":
        return ev
    if kind == "inbox":
        data = {k: v for k, v in (ev.get("data") or {}).items()
                if k not in ("actions", "image", "snapshot_url", "clip_url", "thumb_url")}
        text = f"{data.get('title') or ''} {data.get('body') or ''}".lower()
        if data.get("cat") == "doorbell" or (data.get("cat") == "cameras" and "doorbell" in text):
            data["cat"] = "doorbell"
            return {"type": "inbox", "data": data}
        cat, level = data.get("cat"), data.get("level")
        if level == "act_now" and cat in _TV_ALERTS:
            return {"type": "inbox", "data": data}
        if not s.is_child_like and level == "decision" and cat == "requests":
            return {"type": "inbox", "data": data}
        return None
    return None


def _shape_tv_watching(h: Hub, s: Subject, ev: dict[str, Any]) -> dict[str, Any] | None:
    """Phones learn what a TV is playing. A child only sees their own room and their own TV."""
    if s.role == "pending":
        return None
    data = dict(ev.get("data") or {})
    data.pop("health", None)
    shaped = {"type": "tv", "data": data}
    if s.role in ("admin", "adult", "tablet") or h.policy.has(s, "media.all"):
        return shaped
    room = data.get("room")
    if s.tv_device and data.get("media_entity") == s.tv_device:
        return shaped
    if s.is_child_like:
        return shaped if room and room in s.areas else None
    areas = h.home.visible_areas(s)
    if room and (areas is None or room in areas):
        return shaped
    return None


@router.get("/events")
async def events(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> StreamingResponse:
    s = c.subject

    def accept(ev: dict[str, Any]) -> dict[str, Any] | None:
        return shape_event(h, s, ev)

    sub = h.bus.subscribe(accept, s.person_id, s.device_id, tv=s.tv)

    async def stream():
        try:
            hello: dict[str, Any] = {"role": s.role}
            if s.tv:
                hello["kind"] = "tv"
                hello["tv"] = s.tv_id
            yield sse({"type": "hello", "data": hello})
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
    # Sent by widgets, Control Center, CarPlay and the Watch (all on an unlocked phone or a worn Watch).
    # Actions listed in hub.quick_actions then skip the Face ID step.
    quick: bool = False


@router.post("/actions")
async def actions(body: ActionBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if body.action.startswith("_") or any(k.startswith("_") for k in body.params):
        raise error(400, "bad_params", "Unknown parameter")
    if body.action == "media.play":
        raise error(400, "use_media_play", "Use POST /v1/media/play")
    return await perform(request, c, h, body.action, body.target, body.params, quick=body.quick)


async def perform(request: Request, c: Caller, h: Hub, action: str, target: str | None,
                  params: dict[str, Any], *, quick: bool = False) -> dict[str, Any]:
    try:
        plan = h.actions.plan(c.subject, action, target, params)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message, **exc.extra) from None
    d = plan.decision
    if not d.allowed:
        h.db.log(action, "denied", person_id=c.subject.person_id, device_id=c.device["id"], target=target,
                 params={k: v for k, v in params.items() if not k.startswith("_")}, detail=d.reason)
        raise error(403, "forbidden", "Ask a parent" if d.ask_parent else "Not allowed", ask_parent=d.ask_parent)
    quick_ok = (quick and d.tier == "secure" and action in h.config.hub.quick_actions
                and c.subject.role in ("admin", "adult"))
    if not quick_ok:
        await require_confirm(request, h, c, d.tier)
    try:
        via = "ask" if request.headers.get("x-tc-via") == "ask" else "quick" if quick_ok else "app"
        return await h.actions.run(c.subject, plan, source=via)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message) from None


def dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))
