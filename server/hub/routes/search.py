"""GET /v1/search: one search across the house, as the person asking (see hub/search.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from .. import search as engine
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1", tags=["search"])


def _assistant_ready(request: Request) -> bool:
    return any(getattr(r, "path", None) == "/v1/assistant" for r in request.app.routes)


@router.get("/search")
async def search(request: Request, q: str = "", types: str | None = None, limit: int = 8,
                 c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Results grouped by type, best group first. `types` (comma list) narrows it, e.g. to the
    `pending` types of an earlier answer. Each item's `action`, when present, is accepted by
    POST /v1/actions as-is, except `batch`, whose `steps` are each such an action."""
    if c.subject.role == "pending":
        raise error(403, "forbidden", "This phone isn't assigned to anyone yet")
    want = {t for t in (types or "").split(",") if t in engine.TYPES or t == "command"} or None
    return await engine.run(h, c.subject, q, want, max(1, min(limit, 30)), assistant=_assistant_ready(request))
