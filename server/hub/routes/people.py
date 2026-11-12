"""Push registration, inbox, people and devices, activity log, systems, VPN."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..home import iso
from ..notify import CATEGORIES, DEFAULT_PREFS, Notifier
from ..policy import ROLES
from ..state import Hub
from ..vpn import VpnError
from .deps import Caller, caller, error, hub, need, require_confirm

router = APIRouter(prefix="/v1", tags=["people"])


# -- push and preferences -------------------------------------------------
class PushBody(BaseModel):
    apns_token: str = Field(max_length=200, pattern="^[0-9a-fA-F]+$")
    env: str = Field(default="production", pattern="^(sandbox|production)$")
    prefs: dict[str, bool] | None = None


@router.post("/devices/push")
async def register_push(body: PushBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    prefs = {**DEFAULT_PREFS, **(c.device.get("prefs") or {}), **(body.prefs or {})}
    h.db.update("devices", "id", c.device["id"], {"apns_token": body.apns_token, "apns_env": body.env, "prefs": prefs})
    return {"prefs": prefs, "categories": _categories_for(h, c)}


def _categories_for(h: Hub, c: Caller) -> list[dict[str, Any]]:
    admin_only = {"systems", "devices"}
    return [cat for cat in CATEGORIES if cat["id"] not in admin_only or c.subject.role == "admin"]


class PrefsBody(BaseModel):
    prefs: dict[str, bool]


@router.post("/devices/prefs")
async def set_prefs(body: PrefsBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    known = {cat["id"] for cat in CATEGORIES}
    prefs = {**DEFAULT_PREFS, **(c.device.get("prefs") or {}), **{k: v for k, v in body.prefs.items() if k in known}}
    h.db.update("devices", "id", c.device["id"], {"prefs": prefs})
    return {"prefs": prefs, "categories": _categories_for(h, c)}


class ActivityBody(BaseModel):
    start_token: str | None = Field(default=None, max_length=300)
    activity_id: str | None = Field(default=None, max_length=100)
    token: str | None = Field(default=None, max_length=300)


@router.post("/devices/activity")
async def live_activity(body: ActivityBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Live Activity tokens for the holiday lights. Stored per device; the show
    player integration reads them when it pushes `live_state()` updates."""
    act = dict(c.device.get("activity") or {})
    if body.start_token:
        act["start_token"] = body.start_token
    if body.activity_id and body.token:
        act.setdefault("tokens", {})[body.activity_id] = body.token
    if body.activity_id and body.token is None:
        act.get("tokens", {}).pop(body.activity_id, None)
    h.db.update("devices", "id", c.device["id"], {"activity": act})
    return {"ok": True}


# -- inbox ---------------------------------------------------------------
@router.get("/inbox")
async def inbox(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if not c.subject.person_id:
        return {"items": []}
    rows = h.db.all("SELECT * FROM inbox WHERE person_id=? AND acked_at IS NULL ORDER BY ts DESC LIMIT 200",
                    [c.subject.person_id])
    order = {"act_now": 0, "decision": 1, "maintenance": 2, "fyi": 3}
    rows.sort(key=lambda r: (order.get(r["level"], 9), -r["ts"]))
    return {"items": [Notifier.item(r) for r in rows]}


def _inbox_row(h: Hub, c: Caller, item_id: str) -> dict[str, Any]:
    row = h.db.one("SELECT * FROM inbox WHERE id=? AND person_id=?", [item_id, c.subject.person_id])
    if row is None:
        raise error(404, "not_found", "That notice is gone")
    return row


@router.post("/inbox/{item_id}/ack")
async def inbox_ack(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    row = _inbox_row(h, c, item_id)
    h.db.update("inbox", "id", row["id"], {"acked_at": time.time()})
    return {"ok": True}


class InboxAct(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/inbox/{item_id}/act")
async def inbox_act(item_id: str, body: InboxAct, request: Request, c: Caller = Depends(caller),
                    h: Hub = Depends(hub)) -> dict[str, Any]:
    """Buttons on a notice. The notice's ref says what it is about."""
    row = _inbox_row(h, c, item_id)
    ids = [a["id"] for a in row.get("actions") or []]
    if body.action not in ids:
        raise error(400, "bad_action", "That button isn't on this notice")
    ref = row.get("ref") or ""
    from . import media as media_routes

    if ref.startswith("thomascreek://people/devices/"):
        device_id = ref.rsplit("/", 1)[-1]
        if body.action == "assign":
            pid = body.params.get("person_id")
            if not pid:
                raise error(400, "bad_params", "Pick a person")
            await assign_device(device_id, AssignBody(person_id=pid), request, c, h)
        elif body.action == "revoke":
            await revoke_device(device_id, request, c, h)
    elif ref.startswith("thomascreek://requests/"):
        req_id = ref.rsplit("/", 1)[-1]
        await media_routes.decide_request(req_id, body.action, request, c, h)
    _resolve_group(h, row)
    return {"ok": True}


def _resolve_group(h: Hub, row: dict[str, Any]) -> None:
    """Answering a decision clears it from every adult's inbox."""
    if row.get("group_key"):
        h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), row["group_key"]])
    else:
        h.db.update("inbox", "id", row["id"], {"acked_at": time.time()})


# -- people and devices ---------------------------------------------------
def _person_admin(p: dict[str, Any], h: Hub) -> dict[str, Any]:
    return {
        "id": p["id"], "name": p["name"], "role": p["role"], "avatar": p.get("avatar"),
        "ui_style": p.get("ui_style"), "areas": p.get("areas"), "shared_areas": p.get("shared_areas"),
        "grants": p.get("grants"), "denies": p.get("denies"), "tv_device": p.get("tv_device"),
        "family_board_id": p.get("family_board_id"), "presence_entity": p.get("presence_entity"),
        "access_until": iso(p.get("expires_at")),
        "devices": [_device_out(d) for d in h.db.devices(p["id"])],
    }


def _device_out(d: dict[str, Any]) -> dict[str, Any]:
    return {"id": d["id"], "name": d["name"], "model": d.get("model"), "status": d["status"],
            "person_id": d.get("person_id"), "app_version": d.get("app_version"),
            "vpn": bool(d.get("wg_public_key")), "push": bool(d.get("apns_token")),
            "created_at": iso(d["created_at"]), "last_seen_at": iso(d.get("last_seen_at"))}


@router.get("/people")
async def people(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if h.policy.has(c.subject, "people"):
        return {
            "people": [_person_admin(p, h) for p in h.db.people()],
            "unassigned": [_device_out(d) for d in h.db.all("SELECT * FROM devices WHERE status='pending'")],
            "areas": [{"id": a["id"], "name": a["name"]} for a in h.home.areas.values()],
        }
    # Everyone else sees only their own profile and devices.
    p = h.db.person(c.subject.person_id)
    return {"people": [_person_admin(p, h)] if p else [], "unassigned": []}


class PersonBody(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    role: str | None = None
    avatar: str | None = Field(default=None, max_length=200)
    ui_style: str | None = Field(default=None, pattern="^(standard|kid|little)$")
    areas: list[str] | None = None
    shared_areas: list[str] | None = None
    grants: list[dict[str, Any]] | None = None
    denies: list[dict[str, Any]] | None = None
    tv_device: str | None = None
    family_board_id: str | None = None
    presence_entity: str | None = None
    access_until: float | None = None  # unix seconds, guests


def _person_values(body: PersonBody) -> dict[str, Any]:
    if body.role is not None and body.role not in ROLES:
        raise error(400, "bad_role", f"role must be one of {', '.join(ROLES)}")
    vals = body.model_dump(exclude_unset=True)
    if "access_until" in vals:
        vals["expires_at"] = vals.pop("access_until")
    return vals


@router.post("/people")
async def create_person(body: PersonBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    vals = _person_values(body)
    if not vals.get("name") or not vals.get("role"):
        raise error(400, "bad_params", "name and role are required")
    p = h.db.add_person(vals.pop("name"), vals.pop("role"), **vals)
    h.db.log("people.create", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=p["id"])
    return _person_admin(p, h)


@router.patch("/people/{person_id}")
async def update_person(person_id: str, body: PersonBody, request: Request, c: Caller = Depends(caller),
                        h: Hub = Depends(hub)) -> dict[str, Any]:
    is_self = person_id == c.subject.person_id
    vals = _person_values(body)
    if not h.policy.has(c.subject, "people"):
        # Anyone may change their own avatar and name; nothing else.
        if not is_self or set(vals) - {"avatar", "name"}:
            raise error(403, "forbidden", "Only Dylan can change that", ask_parent=c.subject.is_child_like)
    else:
        await require_confirm(request, h, c, "admin")
        if is_self and vals.get("role") not in (None, "admin"):
            raise error(400, "last_admin", "You can't remove your own admin role here")
    if h.db.person(person_id) is None:
        raise error(404, "not_found", "No such person")
    h.db.update("people", "id", person_id, vals)
    h.db.log("people.edit", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=person_id,
             params={"fields": sorted(vals)})
    return _person_admin(h.db.person(person_id), h)  # type: ignore[arg-type]


class AssignBody(BaseModel):
    person_id: str


@router.post("/devices/{device_id}/assign")
async def assign_device(device_id: str, body: AssignBody, request: Request, c: Caller = Depends(caller),
                        h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    d = h.db.device(device_id)
    p = h.db.person(body.person_id)
    if d is None or p is None:
        raise error(404, "not_found", "Device or person not found")
    if d["status"] == "revoked":
        raise error(409, "revoked", "That device was removed; it must enroll again")
    h.db.update("devices", "id", device_id, {"person_id": p["id"], "status": "active"})
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), f"device:{device_id}"])
    h.db.log("device.assign", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=device_id,
             params={"person_id": p["id"]})
    # Tell that phone to sign in again so its new role takes effect now.
    h.bus.publish({"type": "snapshot", "data": {"reason": "role_changed"}})
    return _device_out(h.db.device(device_id))  # type: ignore[arg-type]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(device_id: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    d = h.db.device(device_id)
    if d is None:
        raise error(404, "not_found", "No such device")
    own = d.get("person_id") and d["person_id"] == c.subject.person_id
    if not own:
        need(h, c, "people")
    await require_confirm(request, h, c, "admin" if not own else "secure")
    h.revoke_device(d, reason=f"by {c.subject.name or c.subject.person_id}")
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), f"device:{device_id}"])
    return {"ok": True}


# -- activity and systems -------------------------------------------------
@router.get("/activity")
async def activity(before: int | None = None, person: str | None = None, limit: int = 100,
                   c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "activity")
    rows = h.db.activity(before=before, limit=min(max(limit, 1), 500), person_id=person)
    names = {p["id"]: p["name"] for p in h.db.people()}
    items = [{
        "id": r["id"], "t": iso(r["ts"]), "who": names.get(r["person_id"] or "", None), "person_id": r["person_id"],
        "device_id": r["device_id"], "action": r["action"], "target": r["target"], "params": r["params"],
        "outcome": r["outcome"], "detail": r["detail"], "source": r["source"],
    } for r in rows]
    return {"items": items, "next_before": rows[-1]["id"] if len(rows) == limit else None}


@router.get("/systems")
async def systems(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    s = h.home.systems or {}
    return {
        "storage": s.get("storage", []),
        "apps": s.get("apps", {}),
        "alerts": s.get("alerts", []),
        "services": h.health(),
        "hub": {"uptime_s": int(time.time() - h.started_at), "dry_run": h.config.hub.dry_run,
                "live_actions": h.config.hub.live_actions, "push": h.apns.enabled, "vpn": h.vpn.enabled,
                "listeners": h.bus.count},
    }


# -- VPN -----------------------------------------------------------------
class VpnBody(BaseModel):
    wg_public_key: str = Field(max_length=64)


@router.post("/vpn/config")
async def vpn_config(body: VpnBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.device["status"] != "active":
        raise error(403, "pending", "Waiting for Dylan to say whose device this is")
    try:
        cfg = h.vpn.config_for(c.device, body.wg_public_key)
    except VpnError as exc:
        raise error(503 if not h.vpn.enabled else 400, "vpn_unavailable", str(exc)) from None
    h.db.log("vpn.config", "ok", person_id=c.subject.person_id, device_id=c.device["id"])
    return cfg
