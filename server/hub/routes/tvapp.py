"""Thomas Creek TV, the household IPTV app on the Android TV boxes (see hub/tvapp.py). Admins only ("systems").

GET    /v1/tvapp                         latest build, pinned signer, boxes with their installed version
POST   /v1/tvapp/check                   look for a new build on GitHub now
POST   /v1/tvapp/boxes                   {ip, name?, auto?} add a box (the TV then shows "Allow debugging?")
POST   /v1/tvapp/boxes/{ip}/refresh      reconnect and read the installed version
POST   /v1/tvapp/boxes/{ip}/install      {force?} install or update now (force: even while the app is open)
PATCH  /v1/tvapp/boxes/{ip}              {auto} keep this box updated automatically
DELETE /v1/tvapp/boxes/{ip}              forget the box (the app stays installed on it)
PATCH  /v1/tvapp/tvs/{id}                {status?: approved|revoked, profile?: adult|kids, name?} a TV that asked for its source
DELETE /v1/tvapp/tvs/{id}                forget a TV (it can ask again)
PUT    /v1/tvapp/kids-categories         {categories: [...]} what a kids' TV shows

Called by the TV app itself, from the home network only, no sign-in:
POST   /v1/tv/enroll                     {install_id, name, model?, app_version?, public_key} -> {status, profile}
POST   /v1/tv/config                     {install_id, ts, signature} -> the IPTV source and profile once approved
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from ..state import Hub
from ..tvapp import TvAppError, clean_ip
from .deps import Caller, caller, caller_ip, error, hub, need

router = APIRouter(prefix="/v1/tvapp", tags=["tvapp"])
tv_router = APIRouter(prefix="/v1/tv", tags=["tvapp"])


def _err(exc: TvAppError):
    return error(exc.status, exc.code, exc.message)


def _log(h: Hub, c: Caller, action: str, target: str | None, outcome: str = "ok") -> None:
    h.db.log(action, outcome, person_id=c.subject.person_id, device_id=c.device.get("id"), target=target, source="app")


@router.get("")
async def overview(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return h.tv_app.overview()


@router.post("/check")
async def check(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        await h.tv_app.check()
    except TvAppError as exc:
        raise _err(exc) from None
    return h.tv_app.overview()


@router.post("/boxes")
async def add_box(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        box = await h.tv_app.add_box(str(body.get("ip") or ""), str(body.get("name") or "")[:80] or None,
                                     auto=bool(body.get("auto", True)), profile=str(body.get("profile") or "adult"))
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.add_box", box["ip"])
    return box


@router.post("/boxes/{ip}/refresh")
async def refresh(ip: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return await h.tv_app.refresh_box(clean_ip(ip))
    except TvAppError as exc:
        raise _err(exc) from None


@router.post("/boxes/{ip}/install")
async def install(ip: str, body: dict[str, Any] = Body(default={}), c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        box = await h.tv_app.install(ip, force=bool(body.get("force")))
    except TvAppError as exc:
        _log(h, c, "tvapp.install", ip, "error")
        raise _err(exc) from None
    _log(h, c, "tvapp.install", ip)
    return box


@router.patch("/boxes/{ip}")
async def set_auto(ip: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return h.tv_app.set_auto(ip, bool(body.get("auto")))
    except TvAppError as exc:
        raise _err(exc) from None


@router.delete("/boxes/{ip}")
async def remove(ip: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        h.tv_app.remove_box(ip)
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.remove_box", ip)
    return {"ok": True}


@router.patch("/tvs/{install_id}")
async def set_tv(install_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        tv = h.tv_app.set_tv(install_id, status=body.get("status"), profile=body.get("profile"),
                             name=str(body.get("name") or "")[:80] or None)
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.set_tv", install_id)
    return tv


@router.delete("/tvs/{install_id}")
async def remove_tv(install_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        h.tv_app.remove_tv(install_id)
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.remove_tv", install_id)
    return {"ok": True}


@router.put("/kids-categories")
async def kids_categories(body: dict[str, Any] = Body(...), c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    cats = body.get("categories")
    if not isinstance(cats, list):
        raise error(400, "bad_request", "categories must be a list")
    return {"categories": h.tv_app.set_kids_categories(cats)}


def _home_only(request: Request, h: Hub) -> str:
    ip = caller_ip(request, h)
    if not h.is_home(ip) or ip is None:
        raise error(403, "not_home", "The TV app talks to the hub only on the home network")
    return ip


@tv_router.post("/enroll")
async def tv_enroll(request: Request, body: dict[str, Any] = Body(...), h: Hub = Depends(hub)) -> dict[str, Any]:
    ip = _home_only(request, h)
    try:
        return await h.tv_app.enroll(ip, str(body.get("install_id") or ""), str(body.get("name") or ""),
                                     body.get("model"), body.get("app_version"), str(body.get("public_key") or ""))
    except TvAppError as exc:
        raise _err(exc) from None


@tv_router.post("/config")
async def tv_config(request: Request, body: dict[str, Any] = Body(...), h: Hub = Depends(hub)) -> dict[str, Any]:
    ip = _home_only(request, h)
    try:
        ts = int(body.get("ts") or 0)
    except (TypeError, ValueError):
        raise error(400, "bad_request", "ts must be unix seconds") from None
    try:
        return h.tv_app.config_for(str(body.get("install_id") or ""), ts, str(body.get("signature") or ""), ip)
    except TvAppError as exc:
        raise _err(exc) from None
