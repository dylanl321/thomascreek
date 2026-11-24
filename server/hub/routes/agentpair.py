"""Agent logins approve test pairings: a TV or emulator asking to join, and a phone or emulator waiting as "pending".

Dylan asked (Nov 24 2026) that Claude agents stop asking him to approve their own test devices. The login needs the
`test.pair` scope (hub/agentlogins.py), works only from the home network or home VPN, and every approval or refusal
is written to the activity log with source "agent" and the login's name, so Dylan can see and undo each one on the web
page. A phone let in this way is a test device for Dylan (kind "test", the "Test" tag in People), the same as a
Simulator that enrolls with a test key; it never gets a child's or another person's access.

An agent also takes its test TVs away again (`GET /v1/agent/tvs`, `DELETE /v1/agent/tv/{install_id}`, Nov 24 2026:
test TVs piled up on the hub and only the web page could remove them). It can remove only a TV an agent login let
in, never one a person approved, and each removal is in the activity log.
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


APPROVALS = ("agent.tv_pair_approve", "agent.tv_approve")
NEAR_S = 2.0


def _mark(h: Hub, install_id: str, row: dict[str, Any]) -> None:
    """Write on the TV's row that an agent login let it in, so only such a TV can be removed by an agent."""
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if tv is None:
        return
    tv["approved_by"] = f"agent:{row['id']}"
    tvs[install_id] = tv
    h.tv_app._save_tvs(tvs)


def _let_in_by_agent(h: Hub, tv: dict[str, Any]) -> bool:
    """True for a TV an agent login approved: marked so, or (TVs let in before the mark existed) approved at the
    moment the activity log has an agent's approval."""
    if str(tv.get("approved_by") or "").startswith("agent:"):
        return True
    at = float(tv.get("approved_at") or 0)
    if not at:
        return False
    marks = ",".join("?" for _ in APPROVALS)
    found = h.db.all(f"SELECT 1 FROM activity WHERE source='agent' AND outcome='ok' AND action IN ({marks})"
                     " AND ts BETWEEN ? AND ? LIMIT 1", [*APPROVALS, at - NEAR_S, at + NEAR_S])
    return bool(found)


def _test_tvs(h: Hub) -> dict[str, dict[str, Any]]:
    return {tid: tv for tid, tv in h.tv_app.tvs().items() if _let_in_by_agent(h, tv)}


@router.get("/tvs")
async def test_tvs(request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """The TVs and emulators an agent login let in: the ones an agent may remove."""
    row = _agent(request, h)
    _log(h, row, "agent.read", "/v1/agent/tvs")
    return {"tvs": [{"install_id": tid, "name": tv.get("name"), "room": tv.get("room"), "model": tv.get("model"),
                     "status": tv.get("status")} for tid, tv in _test_tvs(h).items()]}


@router.delete("/tv/{install_id}")
async def remove_test_tv(install_id: str, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """Forget a test TV an agent login let in. A TV a person approved is never removed here."""
    row = _agent(request, h)
    tv = _test_tvs(h).get(install_id)
    if tv is None:
        h.db.log("agent.tv_remove", "refused", target=install_id, params={"agent": row["name"]}, source="agent")
        raise error(404, "not_found", "No test TV with that id. Read /v1/agent/tvs for the ones an agent let in.")
    try:
        h.tv_app.remove_tv(install_id)
    except TvAppError as exc:
        raise _err(exc) from None
    _log(h, row, "agent.tv_remove", install_id, name=tv.get("name"))
    return {"ok": True, "install_id": install_id}


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
    install_id = next((str(p.get("install_id") or "") for p in h.tv_pair.pending()["pending"]
                       if p.get("pair_id") == pair_id), "")
    try:
        out = h.tv_pair.approve(pair_id, body or {})
    except TvAppError as exc:
        raise _err(exc) from None
    if install_id and out.get("ok") and out.get("status") == "approved":
        _mark(h, install_id, row)
    _log(h, row, "agent.tv_pair_approve", pair_id, status=out.get("status"), install_id=install_id or None)
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
    if tv.get("id"):
        _mark(h, str(tv["id"]), row)
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
