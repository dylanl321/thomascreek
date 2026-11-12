"""Kitchen: ovens, quick presets and tonight's oven setting from the recipe."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..kitchen import oven_plan
from ..state import Hub
from .deps import Caller, caller, error, hub
from .home import perform

router = APIRouter(prefix="/v1", tags=["kitchen"])


def _need(h: Hub, c: Caller) -> None:
    if not h.policy.has(c.subject, "appliances"):
        raise error(403, "forbidden", "Not allowed")


@router.get("/kitchen")
async def kitchen(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _need(h, c)
    return await h.kitchen.summary(c.subject)


class ReadBody(BaseModel):
    steps: list[str] = Field(default_factory=list, max_length=80)
    text: str | None = Field(default=None, max_length=20000)
    cook_time: str | None = Field(default=None, max_length=60)
    title: str | None = Field(default=None, max_length=120)


@router.post("/kitchen/read-recipe")
async def read_recipe(body: ReadBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Find the oven setting in any recipe text (pasted, shared or from another app)."""
    _need(h, c)
    steps = [s[:2000] for s in body.steps]
    if body.text:
        steps += [ln for ln in body.text.splitlines() if ln.strip()][:200]
    return {"oven": oven_plan(steps, body.cook_time, body.title)}


async def inbox_action(request: Request, c: Caller, h: Hub, eid: str, action: str) -> None:
    """Buttons on oven notices: "Start 25 min timer", "Turn off"."""
    if eid not in h.kitchen.ovens():
        raise error(404, "not_found", "That oven isn't in the house")
    if action == "off":
        await perform(request, c, h, "oven.set", eid, {"on": False})
    elif action == "timer":
        sess = h.kitchen.sessions.get(eid)
        minutes = (sess.timer_min if sess else None) or 0
        if not minutes:
            raise error(409, "no_timer", "There's no timer for this oven")
        await perform(request, c, h, "oven.timer", eid, {"minutes": minutes})
