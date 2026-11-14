"""Fleet check-ins and the commands an admin asks a TV to run."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from ..tvfleet import FleetError, ack, audit, command, command_detail, device, devices, history, kill_list, policy, report, rollout, save_shot, set_kill_list, set_policy, shell, shot_path, start_rollout, wait
from .deps import Caller, caller, caller_ip, error, hub, need
from .tvapp import _own_tv

router = APIRouter(prefix="/v1/tv", tags=["tv"])


def _fleet(exc: FleetError) -> None:
    code = {400: "bad_request", 403: "not_home", 404: "not_found"}.get(exc.status, "bad_request")
    raise error(exc.status, code, exc.message) from None


@router.post("/fleet/report")
async def tv_report(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    try:
        return report(h, _own_tv(c), body or {})
    except FleetError as exc:
        _fleet(exc)


@router.get("/manage")
async def tv_manage(c: Caller = Depends(caller), h=Depends(hub), timeout: float = 20) -> dict[str, Any]:
    install_id = _own_tv(c)
    return {"commands": await wait(h, install_id, min(25.0, max(0.0, timeout)))}


@router.post("/manage/ack")
async def tv_manage_ack(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    try:
        return ack(h, _own_tv(c), body or {})
    except FleetError as exc:
        _fleet(exc)


@router.post("/fleet/screenshot")
async def tv_screenshot(request: Request, c: Caller = Depends(caller), h=Depends(hub), command: str = "") -> dict[str, Any]:
    try:
        save_shot(h, _own_tv(c), command, await request.body())
    except FleetError as exc:
        _fleet(exc)
    return {"ok": True}


@router.get("/fleet")
async def tv_fleet(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return devices(h)


@router.get("/fleet/kill-list")
async def tv_kill_list(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return kill_list(h)


@router.put("/fleet/kill-list")
async def tv_kill_list_save(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return set_kill_list(h, list((body or {}).get("packages") or []))
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/policy")
async def tv_policy(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return policy(h)


@router.put("/fleet/policy")
async def tv_policy_save(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return set_policy(h, body or {})
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/rollout")
async def tv_rollout(c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    return rollout(h)


@router.post("/fleet/rollout")
async def tv_rollout_start(body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return start_rollout(h, body or {})
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/audit")
async def tv_audit(c: Caller = Depends(caller), h=Depends(hub), limit: int = 50) -> dict[str, Any]:
    need(h, c, "systems")
    return audit(h, limit)


@router.get("/fleet/{install_id}")
async def tv_device(install_id: str, c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return device(h, install_id)
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/{install_id}/history")
async def tv_history(install_id: str, c: Caller = Depends(caller), h=Depends(hub), metric: str = "cpu", hours: int = 24) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return history(h, install_id, metric, hours)
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/{install_id}/screenshot")
async def tv_shot(install_id: str, c: Caller = Depends(caller), h=Depends(hub)) -> FileResponse:
    need(h, c, "systems")
    try:
        return FileResponse(shot_path(h, install_id), media_type="image/png")
    except FleetError as exc:
        _fleet(exc)


@router.get("/fleet/{install_id}/commands/{command_id}")
async def tv_command_detail(install_id: str, command_id: str, c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return command_detail(h, install_id, command_id)
    except FleetError as exc:
        _fleet(exc)


@router.post("/fleet/{install_id}/shell")
async def tv_shell(install_id: str, body: dict[str, Any], request: Request, c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return shell(h, install_id, body or {}, c.subject.name, h.is_home(caller_ip(request, h)))
    except FleetError as exc:
        _fleet(exc)


@router.post("/fleet/{install_id}/command")
async def tv_command(install_id: str, body: dict[str, Any], c: Caller = Depends(caller), h=Depends(hub)) -> dict[str, Any]:
    need(h, c, "systems")
    try:
        return command(h, install_id, str((body or {}).get("action") or ""), (body or {}).get("params") or {}, c.subject.name)
    except FleetError as exc:
        _fleet(exc)
