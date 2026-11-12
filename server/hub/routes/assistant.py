"""Ask Thomas Creek: chat and voice (see hub/assistant.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..assistant import AssistantError
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/assistant", tags=["assistant"])


class Confirmed(BaseModel):
    id: str = Field(max_length=40)
    ok: bool
    message: str | None = Field(default=None, max_length=300)


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=40)
    screen: str | None = Field(default=None, max_length=200)
    confirmed: list[Confirmed] = Field(default_factory=list, max_length=10)


@router.get("")
async def status(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Whether the assistant is set up, for showing the mic button."""
    return {"ready": h.assistant.ready() and c.subject.role != "pending"}


@router.post("")
async def ask(body: AskBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Ask the house something in plain words. It runs as you, with your limits."""
    if c.subject.role == "pending":
        raise error(403, "forbidden", "This phone isn't assigned to anyone yet")
    try:
        return await h.assistant.ask(
            app=request.app, subject=c.subject, device_id=c.device["id"],
            auth=request.headers.get("authorization", ""), ip=c.ip, text=body.text,
            conversation_id=body.conversation_id, screen=body.screen,
            confirmed=[x.model_dump() for x in body.confirmed])
    except AssistantError as exc:
        raise error(exc.status, exc.error, exc.message) from None
