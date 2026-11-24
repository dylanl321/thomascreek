"""Agent logins approve test pairings: a TV or emulator asking to join, and a phone or emulator waiting as "pending".

Dylan asked (Nov 24 2026) that Claude agents stop asking him to approve their own test devices. The login needs the
`test.pair` scope (hub/agentlogins.py), works only from the home network or home VPN, and every approval or refusal
is written to the activity log with source "agent" and the login's name, so Dylan can see and undo each one on the web
page. A phone let in this way is a test device for Dylan (kind "test", the "Test" tag in People), the same as a
Simulator that enrolls with a test key; it never gets a child's or another person's access.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from .. import agentlogins
from ..state import Hub
from ..tvapp import TvAppError
from .admin_web import _home_only
from .deps import error, hub

router = APIRouter(prefix="/v1/agent", tags=["agent"])
SCOPE = "test.pair"


def _agent(request: Request, h: Hub) -> dict[str, Any]:
    _home_only(request, h)
    row = agentlogins.check(h.db, agentlogins.bearer(request.headers), SCOPE)
    if row is None:
        h.db.log("agent.pair", "refused", target=request.url.path, params={"scope": SCOPE}, source="agent")
        raise error(401, "unauthorized", "This agent login is unknown, turned off, expired or not allowed to do this")
    return row


def _log(h: Hub, row: dict[str, Any], action: str, target: str, **params: Any) -> None:
    h.db.log(action, "ok", target=target, params={"agent": row["name"], "agent_id": row["id"], **params},
             source="agent")


def _err(exc: TvAppError):
    return error(exc.status, exc.code, exc.message)


def _pending_phones(h: Hub) -> list[dict[str, Any]]:
    rows = h.db.all("SELECT id, name, model, platform, app_version, created_at FROM devices WHERE status='pending'"
                    " ORDER BY created_at DESC")
    return [dict(r) for r in rows]


@router.get("/pairings")
async def pairings(request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """Everything waiting to join: TVs and emulators (by pair id), and phones or emulators (by device id)."""
    row = _agent(request, h)
    _log(h, row, "agent.read", "/v1/agent/pairings")
    return {"tvs": h.tv_pair.pending()["pending"], "phones": _pending_phones(h)}


@router.post("/tv/pair/{pair_id}/approve")
async def approve_tv(pair_id: str, request: Request, body: dict[str, Any] | None = None,
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """Let in the TV or emulator that asked to join. Body as on the phone: optional profile, room, name."""
    row = _agent(request, h)
    try:
        out = h.tv_pair.approve(pair_id, body or {})
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, row, "agent.tv_pair_approve", pair_id, status=out.get("status"))
    return out


@router.post("/tv/pair/{pair_id}/deny")
async def deny_tv(pair_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    row = _agent(request, h)
    out = h.tv_pair.deny(pair_id)
    _log(h, row, "agent.tv_pair_deny", pair_id, status=out.get("status"))
    return out


@router.post("/tv/approve")
async def approve_tv_code(request: Request, body: dict[str, Any], h: Hub = Depends(hub)) -> dict[str, Any]:
    """Approve the TV app that shows this six-letter code on screen (the install-by-code path)."""
    row = _agent(request, h)
    code = str(body.get("code") or "")
    try:
        tv = h.tv_app.approve_code(code, room=body.get("room"), profile=body.get("profile"))
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, row, "agent.tv_approve", str(tv.get("id") or code.upper()))
    return tv


@router.post("/devices/{device_id}/approve")
async def approve_phone(device_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """Let a waiting phone or emulator in as one of Dylan's test devices."""
    row = _agent(request, h)
    device = h.db.device(device_id)
    if device is None or device["status"] != "pending":
        raise error(404, "not_found",
                    "No device is waiting with that id. Read /v1/agent/pairings for the current ones.")
    admins = sorted(h.db.people_with_role("admin"), key=lambda p: float(p.get("created_at") or 0))
    if not admins:
        raise error(409, "no_admin", "The hub has no admin to give the test device to")
    owner = admins[0]["id"]
    h.db.update("devices", "id", device_id, {"person_id": owner, "status": "active", "kind": "test"})
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL",
                 [time.time(), f"device:{device_id}"])
    _log(h, row, "agent.device_approve", device_id, name=device.get("name"), model=device.get("model"))
    return {"ok": True, "device_id": device_id, "status": "active", "kind": "test"}


@router.post("/devices/{device_id}/deny")
async def deny_phone(device_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    row = _agent(request, h)
    device = h.db.device(device_id)
    if device is None or device["status"] != "pending":
        raise error(404, "not_found", "No device is waiting with that id")
    h.db.update("devices", "id", device_id, {"status": "revoked"})
    _log(h, row, "agent.device_deny", device_id)
    return {"ok": True, "device_id": device_id, "status": "revoked"}
