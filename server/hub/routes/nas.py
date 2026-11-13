"""The NAS (TrueNAS) section: status, pools, alerts, apps, logs and job progress.

Reads only; every change goes through POST /v1/actions (nas.app, nas.alert,
nas.scrub, nas.snapshot, nas.service) so the admin tier's Face ID applies.
Admin only (the "systems" capability). The TrueNAS key never leaves the hub.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..adapters.base import AdapterError
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1/nas", tags=["nas"])

APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def _ready(h: Hub, c: Caller) -> None:
    need(h, c, "systems")
    if not h.truenas.enabled:
        raise error(409, "not_set_up", "The NAS isn't set up on the hub")


def _wrap(exc: AdapterError):
    msg = str(exc)
    if "API key" in msg:
        return error(409, "not_set_up", msg)
    return error(502, "nas_error", msg)


@router.get("")
async def overview(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """System, pools with drives, open alerts, app counts and sharing services."""
    _ready(h, c)
    try:
        out = await h.truenas.overview()
    except AdapterError as exc:
        raise _wrap(exc) from None
    out["truenas_url"] = (h.config.truenas.url or "").rstrip("/") or None
    return out


@router.get("/apps")
async def apps(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    try:
        rows = await h.truenas.apps()
    except AdapterError as exc:
        raise _wrap(exc) from None
    return {"apps": rows}


@router.get("/apps/{name}")
async def app(name: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _ready(h, c)
    if not APP_NAME.fullmatch(name):
        raise error(400, "bad_target", "Unknown app name")
    try:
        out = await h.truenas.app(name)
    except AdapterError as exc:
        raise _wrap(exc) from None
    if out is None:
        raise error(404, "not_found", f"{name} isn't on the NAS")
    return out


@router.get("/apps/{name}/logs")
async def logs(name: str, container: str | None = Query(default=None, max_length=128),
               tail: int = Query(default=300, ge=10, le=2000),
               c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The last lines of one of the app's containers (the first one unless `container` names another)."""
    _ready(h, c)
    if not APP_NAME.fullmatch(name):
        raise error(400, "bad_target", "Unknown app name")
    try:
        return await h.truenas.logs(name, container, tail)
    except AdapterError as exc:
        if "No such app" in str(exc):
            raise error(404, "not_found", f"{name} isn't on the NAS") from None
        raise _wrap(exc) from None


@router.get("/jobs/{job_id}")
async def job(job_id: int, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Progress of a start, stop, update or service change the app kicked off."""
    _ready(h, c)
    try:
        out = await h.truenas.job(job_id)
    except AdapterError as exc:
        raise _wrap(exc) from None
    if out is None:
        raise error(404, "not_found", "No such job")
    if out.get("state") in ("SUCCESS", "FAILED", "ABORTED"):
        h.home.mark_nas_dirty()
    return out
