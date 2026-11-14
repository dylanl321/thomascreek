"""A TV asks to join from the home network. A parent allows it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..state import Hub
from ..tvapp import TvAppError
from .deps import Caller, caller, caller_ip, error, hub

router = APIRouter(prefix="/v1/tv/pair", tags=["tv"])


def _home(request: Request, h: Hub) -> str:
    ip = caller_ip(request, h)
    if not h.is_home(ip) or ip is None:
        raise error(403, "not_home", "The TV app talks to the hub only on the home network")
    return ip


def _admin(c: Caller) -> None:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only Dylan can change this")


def _err(exc: TvAppError):
    return error(exc.status, exc.code, exc.message)


@router.post("")
async def request_pair(request: Request, body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    ip = _home(request, h)
    try:
        return h.tv_pair.request(ip, body)
    except TvAppError as exc:
        raise _err(exc) from None


@router.get("")
async def list_pending(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    return h.tv_pair.pending()


@router.get("/{pair_id}")
async def poll_pair(pair_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    _home(request, h)
    return h.tv_pair.poll(pair_id)


@router.post("/{pair_id}/approve")
async def approve_pair(pair_id: str, body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    try:
        return h.tv_pair.approve(pair_id, body)
    except TvAppError as exc:
        raise _err(exc) from None


@router.post("/{pair_id}/deny")
async def deny_pair(pair_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    return h.tv_pair.deny(pair_id)
