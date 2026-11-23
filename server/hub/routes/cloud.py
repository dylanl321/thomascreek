"""The cloud media worker's calls, and its page on the hub web page (see hub/cloudmedia.py)."""

from __future__ import annotations

import html
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import agentlogins, cloudmedia
from ..cloudmedia import CloudError
from ..state import Hub
from .admin_web import _csrf, _form_session, _headers, _home_only, _login_page, _nav, _page, _session
from .deps import caller_ip, error, hub

router = APIRouter(tags=["cloud"])
WORKER_HEADER = "x-tc-worker"


def _err(exc: CloudError):
    return error(exc.status, exc.code, exc.message)


def _home(request: Request, h: Hub) -> str | None:
    ip = caller_ip(request, h)
    if not h.is_home(ip):
        raise error(403, "not_home", "The cloud worker joins over the house's private network only")
    return ip


def worker(request: Request, h: Hub = Depends(hub)) -> None:
    """Every worker call after it joined: the secret it made, checked against the hash the hub keeps."""
    if not cloudmedia.check(h, request.headers.get(WORKER_HEADER) or ""):
        raise error(401, "not_joined", "This worker isn't approved. It asks to join again.")


@router.post("/v1/cloud/join")
async def join(request: Request, body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    ip = _home(request, h)
    try:
        return cloudmedia.join(h, ip, body)
    except CloudError as exc:
        raise _err(exc) from None


@router.get("/v1/cloud/join/{join_id}")
async def poll(join_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    _home(request, h)
    return cloudmedia.poll(h, join_id)


@router.post("/v1/cloud/beat", dependencies=[Depends(worker)])
async def beat(body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    return cloudmedia.beat(h, body)


def _pass_claims(h: Hub, body: dict[str, Any]) -> dict[str, Any]:
    from .tvphone import _claims

    token = str(body.get("pass") or "")
    if not token:
        raise error(400, "bad_params", "Send the pass")
    return _claims(h, token, {"movie", "episode", "live"})


@router.post("/v1/cloud/open", dependencies=[Depends(worker)])
async def open_stream(body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    """The provider addresses behind a phone's pass, on the login leased to that phone, best first.

    Only the approved worker gets them (rule: AGENTS.md, a provider address goes to the TV that holds the session,
    a grown-up's own phone, or this worker). It never logs or returns them. A pass whose login moved on answers
    409, as on the hub relay."""
    from .tvphone import _providers

    if not cloudmedia.enabled(h):
        raise error(503, "off", "Lighter streams are switched off on the hub")
    token = str(body.get("pass") or "")
    claims = _pass_claims(h, body)
    kind = str(claims["k"])
    if kind == "live":
        sources = [url for _p, _s, url in _providers(h, claims, "ts")]
        sources += [url for _p, _s, url in _providers(h, claims, "m3u8")]
        relay = f"/v1/tv/phone/live/{token}/stream.ts"
    else:
        sources = [url for _p, _s, url in _providers(h, claims, str(claims.get("x") or "mp4"))]
        relay = f"/v1/tv/phone/file/{token}"
    h.db.log("cloud.open", "ok", device_id=str(claims.get("dev") or "") or None, params={"kind": kind},
             source="cloud")
    return {"kind": kind, "live": kind == "live", "sources": sources, "relay": relay,
            "expires_at": claims.get("exp")}


@router.post("/v1/cloud/touch", dependencies=[Depends(worker)])
async def touch(body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    """The worker is still reading this pass's stream: keep the phone's lease, or say it moved on (409)."""
    from ..iptvplay import held_elsewhere
    from ..iptvplay import touch as keep

    claims = _pass_claims(h, body)
    pl = str(claims.get("pl") or "")
    holder = f"phone:{claims.get('dev')}"
    if "|" in pl:
        provider, login = pl.split("|", 1)
        if held_elsewhere(h, provider, login, holder):
            raise error(409, "moved", "This stream moved on")
        keep(h, holder)
    return {"ok": True}


# -- agent login (hub/agentlogins.py) --------------------------------------------------------------------------------
def _agent(request: Request, h: Hub, scope: str) -> dict[str, Any]:
    """A Claude agent's login, home network or home VPN only, holding `scope`. Every refusal is logged too."""
    _home_only(request, h)
    row = agentlogins.check(h.db, agentlogins.bearer(request.headers), scope)
    if row is None:
        h.db.log("agent.cloud", "refused", target=request.url.path, params={"scope": scope}, source="agent")
        raise error(401, "unauthorized", "This agent login is unknown, turned off, expired or not allowed to do this")
    return row


def _by_code(h: Hub, code: str) -> dict[str, Any]:
    want = "".join(ch for ch in code.upper() if ch.isalnum())
    row = next((r for r in cloudmedia.pending(h) if str(r["code"]).upper() == want), None) if want else None
    if row is None:
        raise error(404, "not_found", "No worker is waiting with that code. Read /v1/agent/cloud for the current one.")
    return row


@router.get("/v1/agent/cloud")
async def agent_cloud(request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """What the Cloud media page shows: the worker's state and who is waiting to join (codes, names, when)."""
    row = _agent(request, h, "admin.read")
    h.db.log("agent.read", "ok", target="/v1/agent/cloud", params={"agent": row["name"], "agent_id": row["id"]},
             source="agent")
    return {"status": cloudmedia.status(h), "waiting": cloudmedia.pending(h), "enabled": cloudmedia.enabled(h)}


@router.post("/v1/agent/cloud/approve")
async def agent_approve(request: Request, body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    """Approve the waiting worker that shows this code, as an admin would on the page. Logged with the agent's name."""
    row = _agent(request, h, "cloud.approve")
    wait = _by_code(h, str(body.get("code") or ""))
    try:
        cloudmedia.approve(h, wait["id"])
    except CloudError as exc:
        raise _err(exc) from None
    h.db.log("agent.cloud_approve", "ok", target=wait["id"],
             params={"agent": row["name"], "agent_id": row["id"], "code": wait["code"], "name": wait["name"]},
             source="agent")
    return {"ok": True, "approved": {"code": wait["code"], "name": wait["name"]}}


@router.post("/v1/agent/cloud/deny")
async def agent_deny(request: Request, body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    row = _agent(request, h, "cloud.approve")
    wait = _by_code(h, str(body.get("code") or ""))
    cloudmedia.deny(h, wait["id"])
    h.db.log("agent.cloud_deny", "ok", target=wait["id"],
             params={"agent": row["name"], "agent_id": row["id"], "code": wait["code"]}, source="agent")
    return {"ok": True}


# -- web page ------------------------------------------------------------------------------------------------------
def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/cloud?m={quote(msg)}", status_code=303)


@router.get("/admin/cloud")
async def cloud_page(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    return HTMLResponse(_page_html(h, claims, request.query_params.get("m")), headers=_headers())


@router.post("/admin/cloud/approve")
async def approve(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    try:
        cloudmedia.approve(h, str(form.get("id") or ""))
    except CloudError as exc:
        return _back(exc.message)
    return _back("Approved. The worker starts offering lighter streams within a minute.")


@router.post("/admin/cloud/deny")
async def deny(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    cloudmedia.deny(h, str(form.get("id") or ""))
    return _back("Turned away.")


@router.post("/admin/cloud/forget")
async def forget(request: Request, h: Hub = Depends(hub)) -> Response:
    await _form_session(request, h)
    cloudmedia.forget(h)
    return _back("Removed the worker. It has to ask to join again.")


@router.post("/admin/cloud/switch")
async def switch(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    on = str(form.get("on") or "") == "1"
    cloudmedia.set_enabled(h, on)
    return _back("Lighter streams are on." if on else "Lighter streams are off. Phones play the full stream.")


def _ago(at: Any) -> str:
    if not isinstance(at, (int, float)):
        return "never"
    s = max(0, int(time.time() - at))
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def _page_html(h: Hub, claims: dict[str, Any], flash: str | None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    s = cloudmedia.status(h)
    waiting = cloudmedia.pending(h)
    rows = "".join(
        f"<div class=row><div>{e(w['name'])} <span class=dim>asked {e(_ago(w['at']))}</span>"
        f"<div class=codebig>{e(w['code'])}</div><div class=dim>Approve only if this code is the one in the "
        f"worker's log.</div></div><div style='display:flex;gap:8px'>"
        f"<form method=post action=/admin/cloud/approve><input type=hidden name=csrf value={csrf}>"
        f"<input type=hidden name=id value={e(w['id'])}><button>Approve</button></form>"
        f"<form method=post action=/admin/cloud/deny><input type=hidden name=csrf value={csrf}>"
        f"<input type=hidden name=id value={e(w['id'])}><button class=ghost>Turn away</button></form></div></div>"
        for w in waiting)
    if s["approved"]:
        state = "Up" if s["up"] else "Not heard from"
        busy = (f"{s['sessions'] or 0} of {s['capacity']} streams" if s.get("capacity")
                else f"{s['sessions'] or 0} streams")
        worker_html = (
            f"<div class=row><div>{e(str(s['name'] or 'Cloud worker'))} <span class=dim>{e(state)}, last "
            f"{e(_ago(s['beat_at']))}</span><div class=dim>{e(busy)}"
            f"{', version ' + e(str(s['version'])) if s.get('version') else ''}"
            f"{'' if s.get('public_url') else ', no public address set'}</div></div>"
            f"<form method=post action=/admin/cloud/forget><input type=hidden name=csrf value={csrf}>"
            f"<button class=ghost>Remove</button></form></div>")
    else:
        worker_html = "<div class=row><div>No worker yet<div class=dim>Start it on the cloud server; its request " \
                      "shows up here with a code.</div></div></div>"
    on = s["enabled"]
    switch_html = (f"<div class=row><div>Offer lighter streams to phones<div class=dim>"
                   f"{'On' if on else 'Off'}: a phone with Data saver, or on a slow connection, plays a smaller "
                   f"picture made on the cloud server.</div></div><form method=post action=/admin/cloud/switch>"
                   f"<input type=hidden name=csrf value={csrf}><input type=hidden name=on value={'0' if on else '1'}>"
                   f"<button class={'ghost' if on else ''}>{'Turn off' if on else 'Turn on'}</button></form></div>")
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    waiting_html = f"<h2>Waiting to join</h2><div class='card list'>{rows}</div>" if waiting else ""
    return _page("Cloud media", f"""
{_nav("/admin/cloud", h)}
<h1>Cloud media</h1><div class=dim>Lighter streams for slow connections, made on the cloud server</div>
{flash_html}
{waiting_html}
<h2>Worker</h2><div class='card list'>{worker_html}{switch_html}</div>""")
