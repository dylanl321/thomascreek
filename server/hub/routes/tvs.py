"""The house's TVs for the iPhone app and the assistant (see hub/tvs.py). Admins and adults ("remotes.setup").

GET    /v1/tvs                        every TV: name, room, profile, online, now playing, version, last error
GET    /v1/tvs/{id}                   one TV: the same plus device health, settings and the last command
GET    /v1/tvs/settings/schema        labels, choices and scope of every TV setting (one editor on iPhone and admin page)
PATCH  /v1/tvs/{id}/settings          {name?, room?, profile?, settings?: {key: value|null}} -> config_changed
POST   /v1/tvs/{id}/settings/copy     {from: <install id>} this TV takes another TV's settings, device set included
POST   /v1/tvs/{id}/actions           {action: restart_app|update|open_app|screenshot|play|tv_power|tv_input|tv_volume|cec_check, ...}
GET    /v1/tvs/{id}/screenshot        the last screen picture (PNG)
POST   /v1/tvs/{id}/watching          {person_id?} "I'm watching": no body = me, null = nobody (any person, see below)

Called by the TV app with its bearer:
POST   /v1/tv/settings                {settings: {...}, base_rev?} a change made on the TV; the last write wins
GET    /v1/tv/watching                the hub's guess of who is watching and who the picker offers
POST   /v1/tv/watching                {person_id|null} the picker's answer

Who is watching is decided in hub/tvwho.py. A kids' TV is only a child's; a child names only themselves.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse

from .. import tvfleet, tvs, tvwho
from ..state import Hub
from .deps import Caller, caller, error, hub, need
from .tvapp import _own_tv

router = APIRouter(prefix="/v1/tvs", tags=["tvs"])
tv_router = APIRouter(prefix="/v1/tv", tags=["tvs"])

CAP = "remotes.setup"


def _err(exc: tvs.TvsError):
    return error(exc.status, exc.code, exc.message)


@router.get("")
async def list_tvs(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, CAP)
    return tvs.listing(h)


@router.get("/settings/schema")
async def settings_schema(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Labels, choices and scope of every TV setting, so the iPhone and the admin page draw the same editor."""
    need(h, c, CAP)
    return tvs.schema()


@router.get("/{install_id}")
async def one_tv(install_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, CAP)
    try:
        return tvs.detail(h, install_id)
    except tvs.TvsError as exc:
        raise _err(exc) from None


@router.patch("/{install_id}/settings")
async def change_settings(install_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                          h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, CAP)
    try:
        return tvs.change(h, install_id, body, by="phone")
    except tvs.TvsError as exc:
        raise _err(exc) from None


@router.post("/{install_id}/settings/copy")
async def copy_settings(install_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                        h: Hub = Depends(hub)) -> dict[str, Any]:
    """{from: <install id>}: this TV takes every setting of the other one, its device set included."""
    need(h, c, CAP)
    try:
        return tvs.copy(h, install_id, str((body or {}).get("from") or ""), by="phone")
    except tvs.TvsError as exc:
        raise _err(exc) from None


@router.post("/{install_id}/actions")
async def tv_action(install_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                    h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, CAP)
    action = str((body or {}).get("action") or "")
    try:
        out = await tvs.act(h, install_id, body, c.subject.name)
    except tvs.TvsError as exc:
        h.db.log("tvs.action", "error", person_id=c.subject.person_id, target=install_id,
                 params={"action": action}, detail=exc.message, source="app")
        raise _err(exc) from None
    h.db.log("tvs.action", "ok", person_id=c.subject.person_id, target=install_id,
             params={"action": action, "via": out.get("via")}, source="app")
    if action == "play" and not c.subject.tv:
        tvwho.claim_for_play(h, install_id, c.subject.person_id, c.subject.role)
    return out


@router.post("/{install_id}/watching")
async def watching(install_id: str, body: dict[str, Any] | None = Body(default=None), c: Caller = Depends(caller),
                   h: Hub = Depends(hub)) -> dict[str, Any]:
    """Say who is watching a TV. No person_id means the caller; null means nobody (the TV's own history)."""
    s = c.subject
    if s.tv or not s.person_id:
        raise error(403, "forbidden", "Sign in on your phone to say who's watching")
    body = body if isinstance(body, dict) else {}
    person_id = body["person_id"] if "person_id" in body else s.person_id
    if person_id is not None and not isinstance(person_id, str):
        raise error(400, "bad_request", "person_id is a person id or null")
    try:
        return tvwho.claim(h, install_id, person_id, by="phone", caller={"id": s.person_id, "role": s.role})
    except tvwho.WhoError as exc:
        raise error(exc.status, exc.code, exc.message) from None


@router.get("/{install_id}/screenshot")
async def tv_screenshot(install_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    need(h, c, CAP)
    try:
        path = tvfleet.shot_path(h, install_id)
    except tvfleet.FleetError as exc:
        raise error(exc.status, "not_found", exc.message) from None
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-cache"})


@tv_router.post("/settings")
async def settings_from_tv(body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                           h: Hub = Depends(hub)) -> dict[str, Any]:
    """A setting changed on the TV itself. The last write wins; the answer is what the hub now holds."""
    install_id = _own_tv(c)
    try:
        return tvs.change(h, install_id, {"settings": (body or {}).get("settings") or {}}, by="tv")
    except tvs.TvsError as exc:
        raise _err(exc) from None


@tv_router.get("/watching")
async def watching_for_tv(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Who the hub thinks is watching this TV, and who the "Who's watching?" picker offers (kids' TVs: children)."""
    return tvwho.public(h, _own_tv(c))


@tv_router.post("/watching")
async def picked_on_tv(body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                       h: Hub = Depends(hub)) -> dict[str, Any]:
    """The picker's answer. null is "nobody": the TV keeps its own history. Then ask for a token as that person."""
    install_id = _own_tv(c)
    person_id = (body or {}).get("person_id")
    if person_id is not None and not isinstance(person_id, str):
        raise error(400, "bad_request", "person_id is a person id or null")
    try:
        tvwho.claim(h, install_id, person_id or None, by="tv")
    except tvwho.WhoError as exc:
        raise error(exc.status, exc.code, exc.message) from None
    return tvwho.public(h, install_id)
