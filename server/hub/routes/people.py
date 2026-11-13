"""Push registration, inbox, people and devices, activity log, systems, VPN."""

from __future__ import annotations

import asyncio
import logging

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..home import iso
from ..notify import CATEGORIES, DEFAULT_PREFS, Notifier
from ..policy import ROLES
from ..state import Hub
from ..vpn import VpnError, valid_wg_key
from ..vpn_pia import PiaError
from .deps import Caller, caller, error, hub, need, require_confirm

log = logging.getLogger(__name__)
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
    elif ref.startswith("thomascreek://kitchen/"):
        from . import kitchen as kitchen_routes

        await kitchen_routes.inbox_action(request, c, h, ref.rsplit("/", 1)[-1], body.action)
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
            "test": d.get("kind") == "test", "created_at": iso(d["created_at"]), "last_seen_at": iso(d.get("last_seen_at"))}


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
ACTIVITY_CATEGORIES = {
    "doors": ("garage.", "lock."), "lights": ("light.", "fan.", "switch.", "scene.", "mode.", "lights."),
    "climate": ("climate.",), "media": ("media.", "speaker.", "request."), "network": ("network.", "timer.network"),
    "people": ("people.", "device.", "auth.", "vpn."), "cameras": ("camera.",), "chores": ("chores.",),
}


DOOR_CLASSES = {"door", "garage_door", "window", "opening"}


def _readable(item: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """A hub action row in words people read, with what it opens in the app (`ref`)."""
    action, target = item.get("action") or "", item.get("target")
    if not item.get("detail"):
        who = names.get(target or "", "")
        words = {
            "vpn.config": "Connected the VPN" + (" through Private" if target == "private" else ""),
            "vpn.pool_add": f"Added {target or 'a config'} to the VPN pool",
            "device.enroll": "Joined with a new device", "device.assign": "Assigned a device",
            "device.revoke": "Removed a device", "auth.token": "Signed in",
            "people.create": f"Added {who or 'a person'}", "people.edit": f"Changed {who or 'a person'}'s settings",
            "people.bootstrap_admin": "Set up as the admin",
        }.get(action)
        if words:
            item["detail"] = words
    if action == "people.view_as":
        who = names.get(target or "", "someone")
        item["detail"] = f"Viewed the app as {who}" if item.get("detail") != "stop" else "Stopped viewing as someone else"
    if action.startswith("people.") and target in names:
        item["ref"] = f"thomascreek://person/{target}"
    elif action.startswith(("device.", "auth.")):
        item["ref"] = "thomascreek://people"
    elif action.startswith("vpn."):
        item["ref"] = "thomascreek://vpnpool"
    if "ref" not in item and item.get("person_id") in names:
        item["ref"] = f"thomascreek://person/{item['person_id']}"
    return item


def _activity_ref(eid: str, cat: str | None, person_id: str | None) -> str | None:
    """What a logbook row opens in the app: the lock or garage itself, Doors for a door sensor, the person."""
    domain = eid.split(".", 1)[0]
    if domain in ("lock", "cover", "valve"):
        return f"thomascreek://device/{eid}"
    if cat == "doors":
        return "thomascreek://doors"
    if person_id:
        return f"thomascreek://person/{person_id}"
    return None


def _home_category(h: Hub, e: dict[str, Any]) -> str | None:
    """Which activity category a Home Assistant logbook line belongs to, if any."""
    eid = e.get("entity_id") or ""
    domain = eid.split(".", 1)[0]
    if domain in ("lock", "cover", "alarm_control_panel", "valve"):
        return "doors"
    if domain == "binary_sensor":
        dc = ((h.home.states.get(eid) or {}).get("attributes") or {}).get("device_class")
        return "doors" if dc in DOOR_CLASSES else None
    if domain == "person":
        return "people"
    if domain in ("automation", "script"):
        return "automation"
    return None


async def _logbook_newest_first(h: Hub, entities: list[str], since: float, until: float,
                                limit: int) -> list[dict[str, Any]]:
    """Home Assistant's logbook from `until` back to `since`, one day per request, newest day first.

    HA answers one day unless given an end, and a multi-day ask over every door sensor can take longer
    than a phone will wait. Asking day by day, newest first, shows today straight away and stops once
    there are enough rows or the time budget is spent (keeping what it has)."""
    import datetime as dt

    def iso(t: float) -> str:
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()

    rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + 6
    end = until
    while end > since and len(rows) < limit:
        start = max(since, end - 86400)
        left = deadline - time.monotonic()
        if left <= 0.5:
            break
        try:
            rows += await asyncio.wait_for(h.ha.logbook(iso(start), iso(end), entities=entities), min(4, left))
        except Exception as exc:  # noqa: BLE001
            log.info("logbook %s..%s failed: %r", iso(start), iso(end), exc)
            break
        end = start
    # A line exactly on a day boundary can come back from both days.
    seen: set[tuple[Any, Any, Any]] = set()
    unique = []
    for r in rows:
        k = (r.get("when"), r.get("entity_id"), r.get("message") or r.get("state"))
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return unique


async def _home_events(h: Hub, category: str | None, since: float, limit: int,
                       until: float | None = None) -> list[dict[str, Any]]:
    """Recent door, lock, garage, presence and automation lines from Home Assistant's logbook."""
    import datetime as dt

    if not h.ha.enabled:
        return []
    entities = sorted(eid for eid in h.home.states if _home_category(h, {"entity_id": eid}) is not None
                      and (not category or _home_category(h, {"entity_id": eid}) == category))
    if not entities:
        return []
    # One fetch per minute per window serves every phone; HA's logbook is the slow part.
    key = (int(since // 60), int((until or 0) // 60), category)
    cache = h.__dict__.setdefault("_logbook_cache", {})
    hit = cache.get(key)
    if hit and time.time() - hit[0] < 60:
        rows = hit[1]
    else:
        rows = await _logbook_newest_first(h, entities, since, until or time.time(), limit)
        if len(cache) > 50:
            cache.clear()
        cache[key] = (time.time(), rows)
    # HA person entities → hub people, so a "Dylan left" row opens Dylan.
    people: dict[str, str] = {}
    for p in h.db.people():
        eid = p.get("presence_entity") or f"person.{str(p['name']).split(' ')[0].lower()}"
        people.setdefault(eid, p["id"])
    out = []
    for e in rows:
        cat = _home_category(h, e)
        if cat is None or (category and cat != category):
            continue
        try:
            ts = dt.datetime.fromisoformat(str(e.get("when")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        by_automation = str(e.get("context_entity_id") or "").startswith(("automation.", "script."))
        name = e.get("name") or e.get("entity_id")
        eid = str(e.get("entity_id") or "")
        pid = people.get(eid) if cat == "people" else None
        out.append({
            "id": f"ha:{int(ts * 1000)}:{e.get('entity_id')}", "t": ts, "at": iso(ts), "who": None, "person_id": pid,
            "ref": _activity_ref(eid, cat, pid),
            "device_id": None, "action": e.get("entity_id", "").split(".", 1)[0] + ".changed", "target": e.get("entity_id"),
            "params": {}, "outcome": "ok", "detail": f"{name} {e.get('message') or e.get('state') or ''}".strip(),
            "source": "automation" if by_automation or cat == "automation" else "home",
        })
    out.sort(key=lambda r: r["t"], reverse=True)
    return out[:limit]


@router.get("/activity")
async def activity(before: str | None = None, person: str | None = None, limit: int = 100,
                   category: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Hub actions (who did what in the app) merged with Home Assistant's logbook
    for doors, locks, garage, presence and automations. Newest first.

    `before` is the `next_before` of the previous page (an opaque string)."""
    need(h, c, "activity")
    limit = min(max(limit, 1), 500)
    if category and category not in ACTIVITY_CATEGORIES and category != "automation":
        raise error(400, "bad_category", f"category is one of {', '.join([*ACTIVITY_CATEGORIES, 'automation'])}")
    prefixes = ACTIVITY_CATEGORIES.get(category or "")
    # Cursor: "<epoch seconds>" — everything strictly older than that.
    try:
        before_ts = float(before) if before else None
    except ValueError:
        raise error(400, "bad_cursor", "before must be the next_before of a previous page") from None
    rows = [] if category == "automation" else h.db.activity(limit=limit, person_id=person, prefixes=prefixes,
                                                              before_ts=before_ts)
    names = {p["id"]: p["name"] for p in h.db.people()}
    items = [_readable({
        "id": str(r["id"]), "t": r["ts"], "at": iso(r["ts"]), "who": names.get(r["person_id"] or "", None),
        "person_id": r["person_id"], "device_id": r["device_id"], "action": r["action"], "target": r["target"],
        "params": r["params"], "outcome": r["outcome"], "detail": r["detail"], "source": r["source"],
    }, names) for r in rows]
    if not person:
        now = time.time()
        newest = before_ts or now
        oldest = items[-1]["t"] if len(items) == limit else newest - 3 * 86400
        home = await _home_events(h, category, oldest, limit, until=newest)
        items += [e for e in home if oldest <= e["t"] < newest]
    items.sort(key=lambda r: r["t"], reverse=True)
    items = items[:limit]
    next_before = str(items[-1]["t"]) if len(items) == limit else None
    return {"items": items, "next_before": next_before}


@router.get("/systems")
async def systems(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    s = h.home.systems or {}
    apps = s.get("apps", {})
    return {
        "storage": s.get("storage", []),
        "pools": s.get("storage", []),
        "apps": [{"name": a["name"], "state": a["state"], "update": a["update"]} for a in apps.get("items", [])],
        "apps_summary": {k: v for k, v in apps.items() if k != "items"},
        "alerts": s.get("alerts", []),
        "services": h.health(),
        "hub": {"uptime_s": int(time.time() - h.started_at), "dry_run": h.config.hub.dry_run,
                "live_actions": h.config.hub.live_actions, "push": h.apns.enabled, "vpn": h.vpn.enabled,
                "listeners": h.bus.count},
    }


# -- VPN -----------------------------------------------------------------
class VpnBody(BaseModel):
    # Required for the hub's own WireGuard and for Private; ignored with a pooled (Firewalla) config.
    wg_public_key: str | None = Field(default=None, max_length=64)
    # "home" (the Firewalla, the default for older app builds) or "private" (PIA).
    exit: str = Field(default="home", pattern="^(home|private)$")
    region: str | None = Field(default=None, max_length=64, pattern="^[A-Za-z0-9_.-]+$")


class PoolBody(BaseModel):
    config: str = Field(max_length=4000)


@router.get("/vpn/pool")
async def vpn_pool(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Dylan's view of the Firewalla clients the hub hands out."""
    need(h, c, "people")
    return {"provider": h.config.vpn.provider, "clients": h.vpn.pool_status()}


@router.post("/vpn/pool")
async def vpn_pool_add(body: PoolBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Add a WireGuard client made on the Firewalla (the .conf text, shared into the app)."""
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    try:
        name = h.vpn.add_to_pool(body.config)
    except VpnError as exc:
        raise error(400, "bad_config", str(exc)) from None
    h.db.log("vpn.pool_add", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=name)
    return {"name": name, "clients": h.vpn.pool_status()}


@router.get("/vpn/exits")
async def vpn_exits(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The VPN buttons this person gets, and for Private the regions the app times to find the nearest."""
    private = h.pia.allowed(c.subject.role)
    out: dict[str, Any] = {"exits": [
        {"id": "home", "available": h.vpn.enabled},
        {"id": "private", "available": private, "provider": "pia"},
    ]}
    if private:
        try:
            out["regions"] = await h.pia.public_regions()
        except PiaError as exc:
            out["exits"][1]["available"] = False
            out["exits"][1]["problem"] = str(exc)
    return out


@router.post("/vpn/config")
async def vpn_config(body: VpnBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.device["status"] != "active":
        raise error(403, "pending", "Waiting for Dylan to say whose device this is")
    if c.device.get("kind") == "test":
        raise error(403, "test_device", "Test simulators don't get a VPN client")
    if body.exit == "private":
        if not h.pia.allowed(c.subject.role):
            raise error(403 if h.pia.configured else 503, "vpn_unavailable",
                        "Private VPN isn't set up for you" if h.pia.configured else "Private VPN isn't set up yet")
        if not body.wg_public_key or not valid_wg_key(body.wg_public_key):
            raise error(400, "bad_key", "wg_public_key must be a base64 Curve25519 public key")
        try:
            cfg = await h.pia.config_for(body.wg_public_key, body.region)
        except PiaError as exc:
            h.db.log("vpn.config", "error", person_id=c.subject.person_id, device_id=c.device["id"], target="private")
            raise error(502, "vpn_provider", str(exc)) from None
        cfg["home_ssids"] = h.config.vpn.home_ssids
        h.db.log("vpn.config", "ok", person_id=c.subject.person_id, device_id=c.device["id"],
                 target=f"private:{cfg['region']['id']}")
        return cfg
    try:
        cfg = h.vpn.config_for(c.device, body.wg_public_key)
    except VpnError as exc:
        raise error(503 if not h.vpn.enabled else 400, "vpn_unavailable", str(exc)) from None
    h.db.log("vpn.config", "ok", person_id=c.subject.person_id, device_id=c.device["id"])
    return {**cfg, "exit": "home"}


@router.post("/push/test")
async def push_test(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Send a test notification to the calling device only."""
    if not h.apns.enabled:
        raise error(503, "push_unavailable", "Push isn't set up on the hub yet")
    token = c.device.get("apns_token")
    if not token:
        raise error(409, "no_token", "This device hasn't registered for notifications")
    from ..push import alert_payload

    res = await h.apns.send(token, alert_payload("Test from the house", "Notifications are working.", cat="house"),
                            env=c.device.get("apns_env") or "production")
    if res.token_dead:
        h.db.update("devices", "id", c.device["id"], {"apns_token": None})
    return {"ok": res.ok, "status": res.status, "reason": res.reason}
