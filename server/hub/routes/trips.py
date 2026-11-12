"""Trips from TREK.

Adults see every trip the hub's TREK key can reach, with booking notes. Children
see the same trips minus any listed in `trek.hidden_from_children`, without
booking or stay notes (that is where confirmation codes and door codes live).
The kitchen tablet gets the next trip's countdown and nothing else. Guests get
nothing. Editing happens in TREK itself; `plan_url` is set only for people who
may plan (`trips.plan`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..adapters import trips as shape
from ..adapters.base import AdapterError
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1", tags=["trips"])


def _today(h: Hub):
    return h.policy.now().date()


def _childlike(c: Caller) -> bool:
    return c.subject.role not in ("admin", "adult")


async def _visible(h: Hub, c: Caller) -> list[dict[str, Any]]:
    if not h.trek.enabled:
        raise error(503, "unavailable", "Trips aren't set up yet")
    try:
        raw = await h.trek.trips()
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if _childlike(c):
        hidden = set(h.config.trek.hidden_from_children)
        raw = [t for t in raw if t.get("id") not in hidden]
    return [shape.summary(t, _today(h)) for t in raw]


async def _travellers(h: Hub, trip_id: int) -> list[dict[str, Any]]:
    try:
        return (await h.trek.trip(trip_id)).get("travellers") or []
    except AdapterError:
        return []


@router.get("/trips")
async def trips(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "trips.view")
    items = shape.sort_trips(await _visible(h, c))
    nxt = shape.next_trip(items)
    if c.subject.role == "tablet":
        return {"trips": [nxt] if nxt else [], "next": nxt, "plan_url": None}
    plan = h.policy.has(c.subject, "trips.plan")
    # Who is going, for the cards that are not in the past (a few at most).
    for t in items:
        if t["status"] != "past":
            t["travellers"] = await _travellers(h, t["id"])
        if plan:
            t["plan_url"] = h.trek.plan_url(t["id"])
    return {"trips": items, "next": nxt, "plan_url": h.trek.plan_url() if plan else None}


@router.get("/trips/{trip_id}")
async def trip(trip_id: int, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "trips.view")
    if c.subject.role == "tablet":
        raise error(403, "forbidden", "Not available on this screen")
    if _childlike(c) and trip_id in set(h.config.trek.hidden_from_children):
        raise error(404, "not_found", "No such trip")
    if not h.trek.enabled:
        raise error(503, "unavailable", "Trips aren't set up yet")
    try:
        raw = await h.trek.trip(trip_id)
    except AdapterError as exc:
        if "404" in str(exc):
            raise error(404, "not_found", "No such trip") from None
        raise error(503, "unavailable", str(exc)) from None
    out = shape.detail(raw, _today(h), child=_childlike(c))
    out["plan_url"] = h.trek.plan_url(trip_id) if h.policy.has(c.subject, "trips.plan") else None
    return out


async def calendar_events(h: Hub, c: Caller, days: int) -> list[dict[str, Any]]:
    """Trip days as all-day calendar entries. Empty when TREK is off or unreachable."""
    if not h.trek.enabled or not h.policy.has(c.subject, "trips.view"):
        return []
    try:
        raw = await h.trek.trips()
    except AdapterError:
        return []
    if _childlike(c):
        hidden = set(h.config.trek.hidden_from_children)
        raw = [t for t in raw if t.get("id") not in hidden]
    raw = [t for t in raw if not t.get("archived")]
    return shape.calendar_events(raw, _today(h), days)
