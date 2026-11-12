"""Settings and favorites that follow a person, and "View as"."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..settings import SettingsError
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1", tags=["settings"])


def _fail(exc: SettingsError):
    return error(exc.status, exc.error, exc.message)


def _log_change(h: Hub, c: Caller, what: str, target: str | None = None) -> None:
    # Only edits made for someone else are worth a line in the Activity log.
    if c.viewer is not None:
        h.db.log("settings.edit", "ok", person_id=c.viewer.person_id, device_id=c.device["id"],
                 target=c.subject.person_id, detail=what if target is None else f"{what} {target}")


# -- the caller's own ---------------------------------------------------------

@router.get("/me/settings")
async def my_settings(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.settings.get(c.subject)


class PatchBody(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)


@router.patch("/me/settings")
async def patch_my_settings(body: PatchBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    try:
        out = h.settings.patch(c.subject, body.changes, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_change(h, c, "settings " + ",".join(sorted(body.changes)))
    return out


class MigrateBody(BaseModel):
    local: dict[str, Any] = Field(default_factory=dict)


@router.post("/me/settings/migrate")
async def migrate_my_settings(body: MigrateBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Once per device after the update: its own choices from before settings synced."""
    if c.viewer is not None:
        return h.settings.get(c.subject)
    try:
        return h.settings.migrate(c.subject, body.local, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None


class Label(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    subtitle: str | None = Field(default=None, max_length=300)
    image: str | None = Field(default=None, max_length=300)


@router.put("/me/favorites/{kind}/{item_id:path}")
async def star(kind: str, item_id: str, body: Label | None = None, c: Caller = Depends(caller),
               h: Hub = Depends(hub)) -> dict[str, Any]:
    try:
        out = h.settings.star(c.subject, kind, item_id, True, body.model_dump() if body else None, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_change(h, c, f"star {kind}", item_id)
    return out


@router.delete("/me/favorites/{kind}/{item_id:path}")
async def unstar(kind: str, item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    try:
        out = h.settings.star(c.subject, kind, item_id, False, None, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_change(h, c, f"unstar {kind}", item_id)
    return out


class OrderBody(BaseModel):
    ids: list[str] = Field(max_length=200)


@router.post("/me/favorites/{kind}/order")
async def reorder(kind: str, body: OrderBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    try:
        out = h.settings.reorder(c.subject, kind, body.ids, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_change(h, c, f"reorder {kind}")
    return out


# -- a parent editing a child's (or Dylan editing anyone's) -----------------------

def _other(h: Hub, c: Caller, person_id: str):
    me = c.viewer or c.subject
    person = h.db.person(person_id)
    if person is None or person["role"] == "pending":
        raise error(404, "not_found", "No such person")
    if person_id == me.person_id:
        return c.subject
    if me.role == "admin" or (me.role == "adult" and person["role"] == "child"):
        return h.subject_for_person(person, me.at_home, c.device["id"])
    raise error(403, "forbidden", "Only a parent can change someone else's favorites")


def _log_for(h: Hub, c: Caller, person_id: str, what: str) -> None:
    me = c.viewer or c.subject
    if person_id != me.person_id:
        h.db.log("settings.edit", "ok", person_id=me.person_id, device_id=c.device["id"], target=person_id, detail=what)


@router.get("/people/{person_id}/settings")
async def person_settings(person_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.settings.get(_other(h, c, person_id))


@router.patch("/people/{person_id}/settings")
async def patch_person_settings(person_id: str, body: PatchBody, c: Caller = Depends(caller),
                                h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.viewer is not None:
        raise error(403, "preview_only", "Exit View as first")
    s = _other(h, c, person_id)
    try:
        out = h.settings.patch(s, body.changes, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_for(h, c, person_id, "settings " + ",".join(sorted(body.changes)))
    return out


@router.put("/people/{person_id}/favorites/{kind}/{item_id:path}")
async def star_for(person_id: str, kind: str, item_id: str, body: Label | None = None, c: Caller = Depends(caller),
                   h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.viewer is not None:
        raise error(403, "preview_only", "Exit View as first")
    s = _other(h, c, person_id)
    try:
        out = h.settings.star(s, kind, item_id, True, body.model_dump() if body else None, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_for(h, c, person_id, f"star {kind} {item_id}")
    return out


@router.delete("/people/{person_id}/favorites/{kind}/{item_id:path}")
async def unstar_for(person_id: str, kind: str, item_id: str, c: Caller = Depends(caller),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.viewer is not None:
        raise error(403, "preview_only", "Exit View as first")
    s = _other(h, c, person_id)
    try:
        out = h.settings.star(s, kind, item_id, False, None, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_for(h, c, person_id, f"unstar {kind} {item_id}")
    return out


@router.post("/people/{person_id}/favorites/{kind}/order")
async def reorder_for(person_id: str, kind: str, body: OrderBody, c: Caller = Depends(caller),
                      h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.viewer is not None:
        raise error(403, "preview_only", "Exit View as first")
    s = _other(h, c, person_id)
    try:
        out = h.settings.reorder(s, kind, body.ids, c.device["id"])
    except SettingsError as exc:
        raise _fail(exc) from None
    _log_for(h, c, person_id, f"reorder {kind}")
    return out


# -- View as ------------------------------------------------------------------------

@router.get("/me/view-as")
async def view_as_people(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Who Dylan can preview the app as."""
    me = c.viewer or c.subject
    if me.role != "admin":
        raise error(403, "forbidden", "Only an admin can view the app as someone else")
    people = [{"id": p["id"], "name": p["name"], "role": p["role"], "style": p.get("ui_style"),
               "color": p.get("color")}
              for p in h.db.people() if p["id"] != me.person_id and p["role"] not in ("pending",)]
    return {"people": people}


class ViewAsBody(BaseModel):
    person_id: str | None = None


@router.post("/me/view-as")
async def view_as_mark(body: ViewAsBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The app says when a preview starts and stops, for the Activity log. The preview itself is the header."""
    me = c.viewer or c.subject
    if me.role != "admin":
        raise error(403, "forbidden", "Only an admin can view the app as someone else")
    if body.person_id and h.db.person(body.person_id) is None:
        raise error(404, "not_found", "No such person")
    h.db.log("people.view_as", "ok", person_id=me.person_id, device_id=c.device["id"],
             target=body.person_id, detail="start" if body.person_id else "stop")
    return {"ok": True}
