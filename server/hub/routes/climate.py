"""Thermostat history: indoor temperature, set points and heating/cooling runs over a chosen range."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from ..adapters.base import AdapterError
from ..climate import RANGES
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/climate", tags=["climate"])


@router.get("/{entity_id}/history")
async def history(entity_id: str = Path(max_length=200, pattern=r"^climate\.[a-z0-9_]+$"),
                  range: str = Query("24h", pattern="^(6h|24h|3d|7d)$"),  # noqa: A002
                  c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Anyone who can see the thermostat on Home can see its history (children: their own rooms)."""
    d = h.home.device(entity_id, c.subject)
    if not d or d["kind"] != "climate":
        raise error(404, "not_found", "No such thermostat")
    if not h.ha.enabled:
        raise error(503, "unavailable", "Home Assistant isn't set up")
    assert range in RANGES
    try:
        return await h.climate.history(entity_id, range)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
