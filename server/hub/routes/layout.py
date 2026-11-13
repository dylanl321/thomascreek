"""The house layout everyone sees: hide devices for everyone, and set up each room.

GET  /v1/home/layout          hidden devices, hidden rooms and a clean-up list of long-dead devices
POST /v1/home/hidden          {ids: [...], hidden: true|false} hide or show devices, scenes or appliances
GET  /v1/rooms/{id}/layout    one room for its editor: name, icon, every device and scene (hidden ones too)
PUT  /v1/rooms/{id}/layout    {name?, symbol?, hidden?, order?, add?, remove?}

Admins only ("home.layout"). Hidden things still work for modes, automations and Ask; they're just not
listed in the app. Every change tells all phones to reload the house.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from ..catalog import LayoutError, is_dead
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1", tags=["layout"])


def _changed(h: Hub, c: Caller, action: str, target: str, params: dict[str, Any]) -> None:
    h.home.recompute()
    h.db.log(action, "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=target, params=params)
    h.bus.publish({"type": "snapshot", "data": {"reason": "layout"}})


def _name(h: Hub, eid: str) -> str:
    if eid.startswith("device:"):
        d = h.home.ha_devices.get(eid[7:]) or {}
        card = next((a for a in h.home.appliances(include_hidden=True) if a["id"] == eid), None)
        return (card or {}).get("name") or d.get("name") or "Appliance"
    return (h.home.states.get(eid, {}).get("attributes", {}) or {}).get("friendly_name") or eid


def _row(h: Hub, eid: str) -> dict[str, Any]:
    area = h.home.entity_area.get(eid)
    st = h.home.states.get(eid, {})
    return {"id": eid, "name": _name(h, eid), "kind": eid.split(".", 1)[0] if "." in eid else "appliance",
            "room_id": area, "room": (h.home.areas.get(area) or {}).get("name") if area else None,
            "available": st.get("state") not in ("unavailable", "unknown") if st else True,
            "since": st.get("last_changed")}


@router.get("/home/layout")
async def layout(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "home.layout")
    home = h.home
    hidden = home.layout.hidden
    cleanup = []
    for eid, area in home.entity_area.items():
        st = home.states.get(eid)
        if not st or eid in hidden or not is_dead(st):
            continue
        if home.device(eid, c.subject) is None and not eid.startswith("scene."):
            continue
        if area in home.areas and not home.areas[area].get("hidden"):
            cleanup.append(_row(h, eid))
    cleanup.sort(key=lambda r: (r["room"] or "", r["name"]))
    return {
        "hidden": sorted((_row(h, e) for e in hidden), key=lambda r: (r["room"] or "~", r["name"])),
        "rooms": [{"id": a["id"], "name": a["name"], "symbol": a["symbol"], "hidden": bool(a.get("hidden")),
                   "floor_id": a.get("floor_id")} for a in home.areas.values()],
        "cleanup": cleanup,
    }


@router.post("/home/hidden")
async def set_hidden(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "home.layout")
    ids = body.get("ids")
    if isinstance(body.get("id"), str):
        ids = [body["id"]]
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise error(400, "bad_ids", "Send the ids to hide or show")
    try:
        out = h.home.layout.set_hidden(ids, bool(body.get("hidden", True)))
    except LayoutError as exc:
        raise error(exc.status, exc.error, exc.message) from None
    _changed(h, c, "home.hide" if body.get("hidden", True) else "home.unhide", ids[0], {"count": len(ids)})
    return {"hidden": out}


def _room(h: Hub, area_id: str) -> dict[str, Any]:
    a = h.home.areas.get(area_id)
    if a is None:
        raise error(404, "not_found", "That room isn't in the house")
    return a


@router.get("/rooms/{area_id}/layout")
async def room_layout(area_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "home.layout")
    a = _room(h, area_id)
    home = h.home
    devices = [d for e, ar in home.entity_area.items() if ar == area_id
               and (d := home.device(e, c.subject, include_hidden=True)) is not None]
    order = home.layout.order(area_id, [d["id"] for d in devices])
    by = {d["id"]: d for d in devices}
    moved_in = [e for e, ar in home.layout.moves.items() if ar == area_id]
    return {
        "id": area_id, "name": a["name"], "symbol": a["symbol"], "hidden": bool(a.get("hidden")),
        "ha_name": (home.base_areas.get(area_id) or {}).get("name", a["name"]),
        "devices": [{"id": e, "name": by[e]["name"], "kind": by[e]["kind"], "hidden": by[e].get("hidden", False),
                     "available": by[e]["available"], "moved_in": e in moved_in} for e in order],
        "scenes": home.scenes(area_id, c.subject, include_hidden=True),
    }


@router.put("/rooms/{area_id}/layout")
async def set_room_layout(area_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                          h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "home.layout")
    _room(h, area_id)
    patch = {k: body[k] for k in ("name", "symbol", "hidden", "order", "add", "remove") if k in body}
    for e in patch.get("add") or []:
        if e not in h.home.states:
            raise error(404, "not_found", "That device isn't in the house")
    try:
        h.home.layout.set_room(area_id, patch)
    except LayoutError as exc:
        raise error(exc.status, exc.error, exc.message) from None
    _changed(h, c, "home.room_layout", area_id, {k: (len(v) if isinstance(v, list) else v) for k, v in patch.items()})
    return await room_layout(area_id, c, h)
