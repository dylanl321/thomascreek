"""Trips from TREK.

Adults see every trip the hub's TREK key can reach, with booking notes. Children
see the same trips minus any listed in `trek.hidden_from_children`, without
booking or stay notes (that is where confirmation codes and door codes live).
The kitchen tablet gets the next trip's countdown and nothing else. Guests get
nothing. Changes go through TREK's MCP tools (see the bottom of this file); `plan_url` is set only for people who
may plan (`trips.plan`).
"""

from __future__ import annotations

import re as _re
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..adapters import trips as shape
from ..adapters.base import AdapterError
from ..adapters.trips import TrekRefused
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


# -- Changing trips ------------------------------------------------------------------------------
# TREK's own MCP tools do the work (create_trip, create_and_assign_place, create_day_note,
# create_reservation ...). The hub lists the ones the family uses, runs them as its machine client
# for people allowed to plan trips, and keeps deletes behind DELETE so the app (and the assistant)
# ask first.

TRIP_TOOL = _re.compile(
    r"^(list|get|search|create|update|delete|assign|unassign|move|reorder|set|rate|bulk_update|bulk_delete|import|"
    r"resolve|reverse|link|toggle)_[a-z_]+$|^create_and_assign_place$")
NOT_FOR_HOME = _re.compile(r"member|guest|share|invite|calendar_feed|file|display_settings|notification|photo|journey|"
                           r"airtrail|settlement|freeze|categor|help|addon|copy_trip|leave_trip|export")
DESTRUCTIVE = _re.compile(r"^(delete|bulk_delete|unassign|remove)_")


def _tool_ok(name: str) -> bool:
    return bool(TRIP_TOOL.match(name)) and not NOT_FOR_HOME.search(name)


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _need_plan(h: Hub, c: Caller) -> None:
    need(h, c, "trips.plan")
    if not h.trek.enabled:
        raise error(503, "unavailable", "Trips aren't set up yet")
    if not h.trek.mcp_ready():
        raise error(503, "not_set_up", "Changing trips needs TREK's machine client on the hub's admin page (Trips)")


@router.get("/trip-tools")
async def trip_tools(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """TREK's trip-planning tools with their inputs: add a trip, places, day plans, notes, bookings, packing."""
    _need_plan(h, c)
    try:
        tools = await h.trek.mcp_tools()
    except (AdapterError, TrekRefused) as exc:
        raise error(503, "unavailable", str(exc)) from None
    return {"tools": [{"name": t["name"], "description": (t.get("description") or "")[:300],
                       "input": t.get("inputSchema") or {}, "removes": bool(DESTRUCTIVE.match(t["name"]))}
                      for t in tools if _tool_ok(t.get("name", ""))]}


async def _run_tool(h: Hub, c: Caller, request: Request, name: str, args: dict[str, Any]) -> dict[str, Any]:
    _need_plan(h, c)
    if not _tool_ok(name):
        raise error(404, "not_found", "That isn't a trip-planning tool the hub offers")
    write = not name.startswith(("list_", "get_", "search_"))
    if write and not h.actions.is_live("trips.edit"):
        h.db.log(f"trips.{name}", "dry_run", person_id=c.subject.person_id, device_id=c.subject.device_id)
        return {"ok": True, "dry_run": True, "message": "Practice mode: not sent to TREK"}
    try:
        out = await h.trek.call_tool(name, args)
    except TrekRefused as exc:
        raise error(400, "refused", f"TREK: {exc}") from None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if write:
        h.db.log(f"trips.{name}", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id,
                 detail=str(args.get("title") or args.get("name") or args.get("trip_id") or "")[:120],
                 source="ask" if request.headers.get("x-tc-via") == "ask" else "app")
    return {"ok": True, "result": out}


@router.post("/trip-tools/{name}")
async def trip_tool(name: str, body: ToolCall, request: Request, c: Caller = Depends(caller),
                    h: Hub = Depends(hub)) -> dict[str, Any]:
    """Run one TREK tool (see GET /v1/trip-tools). Removing things uses DELETE instead."""
    if DESTRUCTIVE.match(name):
        raise error(400, "use_delete", "Removing from a trip uses DELETE on this path")
    return await _run_tool(h, c, request, name, body.arguments)


@router.delete("/trip-tools/{name}")
async def trip_tool_remove(name: str, request: Request, body: ToolCall | None = None, c: Caller = Depends(caller),
                           h: Hub = Depends(hub)) -> dict[str, Any]:
    """Run a TREK tool that removes something (delete_place, unassign_place, delete_trip ...)."""
    if not DESTRUCTIVE.match(name):
        raise error(400, "not_a_delete", "Use POST for tools that don't remove anything")
    return await _run_tool(h, c, request, name, (body or ToolCall()).arguments)
