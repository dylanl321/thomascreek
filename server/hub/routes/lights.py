"""Holiday lights: a role-checked passthrough to show_player.py.

The show player stays its own backend. The hub adds its app token (held in the
vault), checks the path against the caller's role, logs commands, and treats
commands as writes (dry-run until `lights.control` is in live_actions).
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ..adapters.base import AdapterError
from ..crypto import CryptoError
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/lights", tags=["holiday lights"])

READ_PATHS = ("status", "api/live", "api/notifications", "house", "wled", "static")
HOP_HEADERS = {"content-encoding", "transfer-encoding", "connection", "content-length", "set-cookie"}


def _is_read(method: str, path: str) -> bool:
    p = path.lstrip("/")
    return method == "GET" and any(p == r or p.startswith(r + "/") for r in READ_PATHS)


def _lights_role(h: Hub, c: Caller) -> str | None:
    s = c.subject
    if h.policy.has(s, "holiday.full"):
        return "admin"
    if h.policy.has(s, "holiday.looks"):
        return "tablet" if s.role == "tablet" else "adult"
    if h.policy.has(s, "holiday.pick"):
        return "child"
    return None


@router.get("/{path:path}", operation_id="lights_get")
@router.post("/{path:path}", operation_id="lights_post")
async def passthrough(path: str, request: Request, h: Hub = Depends(hub)):
    # Web pages (3D house, WLED) take ?access_token= once and keep a cookie.
    token = request.query_params.get("access_token")
    if token and request.method == "GET":
        try:
            h.tokens.verify(token)
        except CryptoError:
            raise error(401, "unauthorized", "Token expired") from None
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "access_token"]
        qs = urlencode(params)
        resp = RedirectResponse(url=f"/v1/lights/{path}" + (f"?{qs}" if qs else ""), status_code=303)
        resp.set_cookie("tc_page", token, httponly=True, secure=True, samesite="strict",
                        max_age=h.config.hub.access_token_ttl_s, path="/v1/lights")
        return resp

    c = caller(request, h)
    role = _lights_role(h, c)
    sp = h.show_player
    if role is None or not sp.path_allowed(role, path):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)

    is_read = _is_read(request.method, path)
    if not is_read:
        params = dict(request.query_params)
        if not h.actions.is_live("lights.control"):
            h.db.log("lights.control", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"],
                     target=path, params=params)
            return {"ok": True, "dry_run": True, "message": "Practice mode: the show was not changed"}
        h.db.log("lights.control", "ok", person_id=c.subject.person_id, device_id=c.device["id"],
                 target=path, params=params)

    body = await request.body() if request.method == "POST" else None
    try:
        upstream = await sp.forward(request.method, path, list(request.query_params.multi_items()), body,
                                    request.headers.get("content-type"))
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
    return Response(upstream.content, status_code=upstream.status_code, headers=headers)
