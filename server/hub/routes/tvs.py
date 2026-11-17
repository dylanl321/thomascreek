"""The house's TVs for the iPhone app and the assistant (see hub/tvs.py). Admins and adults ("remotes.setup").

GET    /v1/tvs                        every TV: name, room, profile, online, now playing, version, last error
GET    /v1/tvs/{id}                   one TV: the same plus device health, settings and the last command
GET    /v1/tvs/settings/schema        labels, choices and scope of every TV setting (one editor on iPhone and admin page)
PATCH  /v1/tvs/{id}/settings          {name?, room?, profile?, settings?: {key: value|null}} -> config_changed
POST   /v1/tvs/{id}/settings/copy     {from: <install id>} this TV takes another TV's settings, device set included
POST   /v1/tvs/{id}/actions           {action: restart_app|update|open_app|screenshot|play|tv_power|tv_input|tv_volume|cec_check, ...}
GET    /v1/tvs/{id}/screenshot        the last screen picture (PNG)

Called by the TV app with its bearer:
POST   /v1/tv/settings                {settings: {...}, base_rev?} a change made on the TV; the last write wins
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse

from .. import tvfleet, tvs
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
    return out


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
