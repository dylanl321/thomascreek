"""The house brain: what it costs, what it's been doing, and its switches. Admins only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/brain", tags=["brain"])


def _admin(c: Caller) -> None:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only an admin can see the house brain")


@router.get("/summary")
async def summary(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """This month's spend (brain and Ask Thomas Creek), runs, cards and how they landed, and every job."""
    _admin(c)
    return h.brain.summary()


@router.get("/runs")
async def runs(job: str | None = Query(default=None, max_length=40), limit: int = Query(default=50, ge=1, le=200),
               before: float | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Newest first. `before` is the last row's ts, for the next page."""
    _admin(c)
    items = h.brain.runs(job=job, limit=limit, before=before)
    return {"items": items, "before": items[-1]["ts"] if len(items) == limit else None}


@router.get("/runs/{run_id}")
async def run(run_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    r = h.brain.run_detail(run_id)
    if r is None:
        raise error(404, "not_found", "No such run")
    return r


class BrainSwitch(BaseModel):
    paused: bool


@router.post("")
async def set_paused(body: BrainSwitch, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Pause or resume every job (config brain.enabled is the hard off switch)."""
    _admin(c)
    h.brain.set_paused(body.paused)
    h.db.log("brain.pause" if body.paused else "brain.resume", "ok", person_id=c.subject.person_id,
             device_id=c.device["id"])
    return h.brain.summary()


class JobSwitch(BaseModel):
    enabled: bool | None = None
    autonomy: str | None = None  # observe | suggest


@router.post("/jobs/{job_id}")
async def set_job(job_id: str, body: JobSwitch, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    if job_id not in h.brain.jobs:
        raise error(404, "not_found", "No such job")
    if body.autonomy is not None and body.autonomy not in ("observe", "suggest"):
        raise error(400, "bad_request", "Jobs can watch or suggest; acting on their own isn't switched on")
    h.brain.set_job(job_id, enabled=body.enabled, autonomy=body.autonomy)
    h.db.log("brain.job", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=job_id,
             params=body.model_dump(exclude_none=True))
    return h.brain.summary()


@router.post("/jobs/{job_id}/run")
async def run_now(job_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Run a job once now (for trying it out). The same rules apply: nothing new, nothing said."""
    _admin(c)
    if job_id not in h.brain.jobs or job_id == "catch_up":
        raise error(404, "not_found", "No such job")
    h.brain._last_hash.pop(f"{job_id}:", None)
    return {"result": await h.brain.run(job_id)}
