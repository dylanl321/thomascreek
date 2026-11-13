"""Thomas Creek TV, the household IPTV app on the Android TV boxes (see hub/tvapp.py). Admins only ("systems").

GET    /v1/tvapp                         latest build, pinned signer, boxes with their installed version
POST   /v1/tvapp/check                   look for a new build on GitHub now
POST   /v1/tvapp/boxes                   {ip, name?, auto?} add a box (the TV then shows "Allow debugging?")
POST   /v1/tvapp/boxes/{ip}/refresh      reconnect and read the installed version
POST   /v1/tvapp/boxes/{ip}/install      {force?} install or update now (force: even while the app is open)
PATCH  /v1/tvapp/boxes/{ip}              {auto} keep this box updated automatically
DELETE /v1/tvapp/boxes/{ip}              forget the box (the app stays installed on it)
PATCH  /v1/tvapp/tvs/{id}                {status?, profile?, name?, room?, media_entity?}
DELETE /v1/tvapp/tvs/{id}                forget a TV (it can ask again)
PUT    /v1/tvapp/kids-categories         {categories: [...]} what a kids' TV shows
PUT    /v1/tvapp/lineup                  replace the channel lineup (systems)

Called by the TV app from the home network. Enroll and config are signed with the
device key. The token exchange is too. Everything after that uses the bearer:
POST   /v1/tv/enroll                     {install_id, name, model?, app_version?, public_key, code?}
GET    /v1/tv/apk/{code}                 home network, the signed APK for a short install code
GET    /v1/tv/update                     bearer, version, sha256 and signer pin
GET    /v1/tv/apk                        bearer, the signed APK for PackageInstaller
GET    /v1/tv/apps                       bearer, {allowed:[packages]} for this profile
POST   /v1/tv/config                     {install_id, ts, signature}  ETag / 304
POST   /v1/tv/token                      {install_id, ts, signature, person_id?} -> bearer
GET    /v1/tv/lineup                     bearer, phones and TVs
POST   /v1/tv/state                      bearer, now playing and health
GET    /v1/tv/commands                   bearer, long-poll tv.play / tv.stop
POST   /v1/tv/commands/ack               bearer, {ids}
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..home import iso
from ..state import Hub
from ..tvapp import TvAppError, clean_ip
from ..tvremote import allowed_packages
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
        extra: dict[str, Any] = {}
        if "room" in body:
            extra["room"] = body.get("room")
        if "media_entity" in body:
            extra["media_entity"] = body.get("media_entity")
        tv = h.tv_app.set_tv(install_id, status=body.get("status"), profile=body.get("profile"),
                             name=str(body.get("name") or "")[:80] or None, **extra)
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


class ChannelIn(BaseModel):
    name: str = Field(max_length=80)
    number: int | None = None
    group: str | None = Field(default=None, max_length=40)
    logo: str | None = Field(default=None, max_length=300)
    kids: bool = False
    hidden: bool = False
    key: str | None = Field(default=None, max_length=140)
    source: str | None = Field(default=None, max_length=40)
    stream_id: str | None = Field(default=None, max_length=80)
    tvg_id: str | None = Field(default=None, max_length=80)
    iptv_org: str | None = Field(default=None, max_length=80)


class LineupIn(BaseModel):
    channels: list[ChannelIn] = Field(max_length=5000)


@router.put("/lineup")
async def put_lineup(body: LineupIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Replace the channel lineup every TV and phone reads."""
    need(h, c, "systems")
    try:
        lineup = h.tv_app.set_lineup([row.model_dump() for row in body.channels])
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.lineup", None)
    return lineup


class SetupIn(BaseModel):
    ip: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=80)
    room: str | None = Field(default=None, max_length=40)
    profile: str = "adult"
    pairing_port: int | None = None
    pairing_code: str | None = Field(default=None, max_length=8)
    home: bool = False
    mode: str = "adb"


class ApproveIn(BaseModel):
    code: str = Field(max_length=8)
    room: str | None = Field(default=None, max_length=40)
    profile: str | None = None


def _with_install_url(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    install = body.get("install")
    if isinstance(install, dict) and install.get("path"):
        install["url"] = str(request.base_url).rstrip("/") + str(install["path"])
    return body


@router.get("/discover")
async def discover(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Android TVs advertising wireless debugging. Empty when none answer; type an address instead."""
    need(h, c, "systems")
    from ..tvsetup import Setup

    return await Setup(h.tv_app).discover()


@router.post("/setup")
async def setup(request: Request, body: SetupIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Install Thomas Creek TV. mode adb uses debugging. mode non-adb returns a short download link instead."""
    need(h, c, "systems")
    from ..tvsetup import Setup

    try:
        worker = Setup(h.tv_app)
        if body.mode == "non-adb":
            out = worker.manual(name=body.name, room=body.room, profile=body.profile)
        else:
            if not body.ip:
                raise TvAppError(400, "bad_ip", "Type the TV's address, or set mode to non-adb")
            out = await worker.run(body.ip, name=body.name, room=body.room, profile=body.profile,
                                   pairing_port=body.pairing_port, pairing_code=body.pairing_code, home=body.home,
                                   mode="adb")
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.setup", body.ip or (out.get("install") or {}).get("code"))
    return _with_install_url(request, out)


@router.get("/setup/code/{code}")
async def setup_by_code(code: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Status for a TV that is installing without debugging."""
    need(h, c, "systems")
    from ..tvsetup import status_of

    box = h.tv_app.boxes().get(f"code:{code.strip().upper()}")
    if box is None:
        raise error(404, "not_found", "That install link has expired")
    return _with_install_url(request, status_of(dict(box, ip=f"code:{code.strip().upper()}")))


@router.post("/approve")
async def approve(body: ApproveIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Approve the TV that is showing this code. Nothing is typed on the TV."""
    need(h, c, "systems")
    try:
        tv = h.tv_app.approve_code(body.code, room=body.room, profile=body.profile)
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.approve", tv.get("id") or body.code)
    return tv


@router.get("/setup/{ip}")
async def setup_status(ip: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Where setup is, and the one thing the person may still need to do on the TV."""
    need(h, c, "systems")
    from ..tvsetup import status_of

    try:
        box = h.tv_app.boxes().get(clean_ip(ip))
    except TvAppError as exc:
        raise _err(exc) from None
    if box is None:
        raise error(404, "not_found", "That box isn't on the list")
    return status_of(dict(box, ip=clean_ip(ip)))


@router.post("/setup/{ip}/rollback-home")
async def rollback_home(ip: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Put the previous launcher back. Thomas Creek TV stays installed."""
    need(h, c, "systems")
    from ..tvsetup import Setup

    try:
        return await Setup(h.tv_app).rollback_home(ip)
    except TvAppError as exc:
        raise _err(exc) from None


@router.post("/catalog/refresh")
async def refresh_catalog(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Download the provider's channels, guide, series and movies again."""
    need(h, c, "systems")
    try:
        await h.tv_catalog.refresh()
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, c, "tvapp.catalog", None)
    return h.tv_catalog.summary()


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
                                     body.get("model"), body.get("app_version"), str(body.get("public_key") or ""),
                                     str(body.get("code") or "") or None)
    except TvAppError as exc:
        raise _err(exc) from None


@tv_router.get("/apk/{code}")
async def apk_by_code(code: str, request: Request, h: Hub = Depends(hub)) -> FileResponse:
    """The signed APK, for Downloader or a browser on the home network. The code expires."""
    _home_only(request, h)
    if h.tv_app.install_link(code) is None:
        raise error(404, "not_found", "That install link has expired")
    try:
        path = h.tv_app.apk_file()
    except TvAppError as exc:
        raise _err(exc) from None
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=path.name)


@tv_router.get("/apk")
async def apk_for_tv(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    """The signed APK for a TV that already has a token, so it can hand the file to PackageInstaller."""
    _home_only(request, h)
    if not c.subject.tv:
        raise error(403, "forbidden", "The TV app downloads its own update")
    try:
        path = h.tv_app.apk_file()
    except TvAppError as exc:
        raise _err(exc) from None
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=path.name)


@tv_router.get("/apps")
async def tv_apps(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Packages this TV may put on its home screen. Kids get the kid apps; adults get the household catalog."""
    _own_tv(c)
    tv = h.tv_app.tvs().get(c.subject.tv_id or "") or {}
    kids = tv.get("profile") == "kids" or c.subject.is_child_like
    return {"allowed": allowed_packages(kids=kids)}


@tv_router.get("/update")
async def tv_update(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Version, sha256 and signer pin for the APK at GET /v1/tv/apk. The TV checks both before installing."""
    _home_only(request, h)
    if not c.subject.tv:
        raise error(403, "forbidden", "The TV app reads its own update")
    try:
        return h.tv_app.update_manifest()
    except TvAppError as exc:
        raise _err(exc) from None


@tv_router.post("/config")
async def tv_config(request: Request, body: dict[str, Any] = Body(...), h: Hub = Depends(hub)) -> Response:
    """Signed config. Send If-None-Match to get 304 when nothing the TV caches has changed."""
    ip = _home_only(request, h)
    try:
        ts = int(body.get("ts") or 0)
    except (TypeError, ValueError):
        raise error(400, "bad_request", "ts must be unix seconds") from None
    try:
        payload = h.tv_app.config_for(str(body.get("install_id") or ""), ts, str(body.get("signature") or ""), ip)
    except TvAppError as exc:
        raise _err(exc) from None
    return _cached(request, payload)


class TokenIn(BaseModel):
    install_id: str = Field(max_length=64)
    ts: int
    signature: str = Field(max_length=200)
    person_id: str | None = Field(default=None, max_length=40)


@tv_router.post("/token")
async def tv_token(request: Request, body: TokenIn, h: Hub = Depends(hub)) -> dict[str, Any]:
    """Exchange the TV's device-key signature for a short-lived bearer.

    The signature covers `tvtoken:<install_id>:<ts>` or, when narrowing to a
    person, `tvtoken:<install_id>:<ts>:<person_id>`. The same timestamp cannot
    be reused. The bearer lasts as long as a phone token and never carries a
    biometric key, so locks, the garage and cameras stay refused.
    """
    ip = _home_only(request, h)
    try:
        subject, device, fingerprint = h.tv_app.begin_token(
            body.install_id, body.ts, body.signature, body.person_id, ip)
    except TvAppError as exc:
        raise _err(exc) from None
    access, exp = h.tokens.issue(device["id"], subject.person_id, subject.role, extra={
        "kind": "tv", "tv": body.install_id, "key": fingerprint,
    })
    person = h.db.person(subject.person_id)
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_at": iso(exp),
        "expires_in": max(0, int(exp - time.time())),
        "role": subject.role,
        "profile": h.tv_app.tvs().get(body.install_id, {}).get("profile"),
        "room": (subject.areas or subject.shared_areas or [None])[0],
        "person": _person(person),
        "capabilities": h.policy.capabilities_for(subject),
        "tv": {"id": body.install_id, "name": device["name"]},
    }


class PlayingIn(BaseModel):
    channel_key: str | None = Field(default=None, max_length=140)
    title: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=80)
    number: int | None = None


class HealthIn(BaseModel):
    app_version: str | None = Field(default=None, max_length=40)
    profile: str | None = Field(default=None, max_length=20)
    epg_age_s: int | None = None
    last_error: str | None = Field(default=None, max_length=300)
    home_crashes: int | None = None


class StateIn(BaseModel):
    playing: PlayingIn | None = None
    foreground: bool = True
    health: HealthIn | None = None
    command_id: str | None = Field(default=None, max_length=48)
    command_ok: bool | None = None
    command_error: str | None = Field(default=None, max_length=300)


@tv_router.post("/state")
async def tv_state(body: StateIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Now playing and a health heartbeat. Phones see a `tv` event."""
    install_id = _own_tv(c)
    try:
        data = h.tv_app.record_state(install_id, body.model_dump())
    except TvAppError as exc:
        raise _err(exc) from None
    h.bus.publish({"type": "tv", "data": data})
    crashes = body.health.home_crashes if body.health else None
    if crashes is not None and crashes >= 2:
        from ..tvsetup import Setup

        ip = (h.tv_app.tvs().get(install_id) or {}).get("ip")
        try:
            await Setup(h.tv_app).note_crashes(ip, crashes)
        except TvAppError:
            pass
    return {"ok": True}


@tv_router.get("/lineup")
async def tv_lineup(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """Channels with stable keys. A child, a guest and a kids' TV only get kids' channels."""
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)
    lineup = h.tv_catalog.lineup(kids_only=c.subject.is_child_like) or h.tv_app.public_lineup(kids_only=c.subject.is_child_like)
    return _cached(request, lineup)


@tv_router.get("/commands")
async def tv_commands(c: Caller = Depends(caller), h: Hub = Depends(hub), timeout: float = 20) -> dict[str, Any]:
    """Long-poll for tv.play and tv.stop. The same commands also arrive as SSE `tv_command`."""
    install_id = _own_tv(c)
    wait = min(25.0, max(0.0, timeout))
    return {"commands": await h.tv_app.wait_commands(install_id, wait)}


class AckIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=20)


@tv_router.post("/commands/ack")
async def tv_commands_ack(body: AckIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Drop commands the TV has applied. Unacked commands stay until they expire (2 minutes)."""
    install_id = _own_tv(c)
    return {"ok": True, "acked": h.tv_app.ack_commands(install_id, body.ids)}


def _own_tv(c: Caller) -> str:
    if not c.subject.tv or not c.subject.tv_id:
        raise error(403, "not_a_tv", "This call is for a TV")
    return c.subject.tv_id


def _person(person: dict[str, Any] | None) -> dict[str, Any] | None:
    if person is None:
        return None
    return {"id": person["id"], "name": person["name"], "role": person["role"],
            "ui_style": person.get("ui_style") or ("kid" if person["role"] == "child" else "standard")}


def _etag(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'


def _cached(request: Request, body: dict[str, Any]) -> Response:
    tag = _etag(body)
    headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)
