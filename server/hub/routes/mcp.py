"""The hub's MCP server and its OAuth sign-in (see hub/mcp.py and hub/mcp_oauth.py)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..mcp import McpError
from ..mcp_oauth import OAuthError
from ..state import Hub
from .admin_web import _headers, _page
from .deps import caller_ip, hub

router = APIRouter(include_in_schema=False)

MAX_BODY = 256 * 1024


def _base(request: Request, h: Hub) -> str:
    return h.mcp_oauth.base(str(request.base_url))


def _unauthorized(request: Request, h: Hub, message: str, status: int = 401) -> JSONResponse:
    meta = f"{_base(request, h)}/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "unauthorized", "message": message}, status_code=status,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


@router.post("/mcp")
async def mcp(request: Request, h: Hub = Depends(hub)) -> Response:
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    try:
        who = h.mcp.sign_in(bearer, caller_ip(request, h))
    except McpError as exc:
        if exc.status == 401:
            return _unauthorized(request, h, exc.message)
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"error": "too_large", "message": "Request too large"}, status_code=413)
    try:
        msg = json.loads(raw)
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                            status_code=400)
    if isinstance(msg, list):  # a batch (older clients)
        out = [r for r in [await h.mcp.handle(request.app, who, m) for m in msg[:20]] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await h.mcp.handle(request.app, who, msg)
    if res is None:
        return Response(status_code=202)
    return JSONResponse(res)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_other() -> Response:
    # No server-sent stream and no sessions: every answer comes back on its POST.
    return Response(status_code=405, headers={"Allow": "POST"})


# -- OAuth ---------------------------------------------------------------------
@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.mcp_oauth.metadata(_base(request, h))


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_resource(request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.mcp_oauth.resource(_base(request, h))


@router.post("/oauth/register")
async def oauth_register(request: Request, h: Hub = Depends(hub)) -> Response:
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "JSON body expected"}, status_code=400)
    try:
        return JSONResponse(h.mcp_oauth.register(body), status_code=201)
    except OAuthError as exc:
        return JSONResponse(exc.body(), status_code=exc.status)


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request, h: Hub = Depends(hub)) -> Response:
    if not h.config.mcp.enabled:
        return HTMLResponse(_page("Not available", "<p>AI connections are turned off on this hub.</p>"),
                            status_code=503, headers=_headers())
    try:
        p = h.mcp_oauth.start(dict(request.query_params))
    except OAuthError as exc:
        return HTMLResponse(_page("Can't connect", f"<p>{exc.description}</p>"), status_code=exc.status,
                            headers=_headers())
    return HTMLResponse(_page("Connect to Thomas Creek", h.mcp_oauth.page_body(p)), headers=_headers())


@router.get("/oauth/poll")
async def oauth_poll(id: str, h: Hub = Depends(hub)) -> Response:
    url = h.mcp_oauth.poll(id)
    if url:
        return JSONResponse({"redirect": url})
    if any(p["id"] == id for p in h.mcp_oauth.pending.values()):
        return JSONResponse({"waiting": True})
    return JSONResponse({"error": "expired"}, status_code=404)


@router.post("/oauth/token")
async def oauth_token(request: Request, h: Hub = Depends(hub)) -> Response:
    raw = (await request.body())[:8192].decode(errors="replace")
    ctype = request.headers.get("content-type", "")
    try:
        form = json.loads(raw) if "json" in ctype else dict(parse_qsl(raw))
        if not isinstance(form, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "invalid_request", "error_description": "Bad body"}, status_code=400)
    try:
        out = h.mcp_oauth.token({k: str(v) for k, v in form.items()})
    except OAuthError as exc:
        return JSONResponse(exc.body(), status_code=exc.status)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
