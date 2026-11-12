"""Smart "Right now" cards: the list, "Not now", and which suggestions a person turned off."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..state import Hub
from .deps import Caller, caller, hub

router = APIRouter(prefix="/v1/smart", tags=["smart"])


@router.get("")
async def smart(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return {"cards": h.smart.cards(c.subject), "weather": h.smart.weather.now()}


class DismissBody(BaseModel):
    id: str = Field(max_length=200)
    rule: str = Field(max_length=40)
    handled: bool = False  # True: the person used the card (Done, Got it); False: "Not now"


@router.post("/dismiss")
async def dismiss(body: DismissBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.smart.dismiss(c.subject, body.id, body.rule, body.handled)


@router.get("/settings")
async def settings(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.smart.settings(c.subject)


class MuteBody(BaseModel):
    rule: str = Field(max_length=40)
    muted: bool


@router.post("/settings")
async def set_settings(body: MuteBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    h.smart.set_muted(c.subject, body.rule, body.muted)
    return h.smart.settings(c.subject)
