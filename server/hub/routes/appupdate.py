"""Is there a newer build of the app? The app asks on launch and when it comes back to the front."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..crypto import CryptoError
from ..state import Hub
from .deps import _token_from, hub

router = APIRouter(prefix="/v1/app", tags=["meta"])


@router.get("/update")
async def app_update(request: Request, build: int | None = Query(default=None, ge=0, le=10_000_000),
                     version: str | None = Query(default=None, max_length=20),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """The newest build TestFlight has, and the oldest build still allowed.

    No sign-in needed (a phone that is still pending, or was just reset, must still hear
    about a required update). When a signed-in phone sends its own `build`, the hub
    remembers it so the web page can show who hasn't updated.
    """
    info = h.app_update.info()
    if build is not None:
        token = _token_from(request, False)
        if token:
            try:
                claims = h.tokens.verify(token)
                h.db.update("devices", "id", claims["sub"],
                            {"app_version": f"{version} ({build})" if version else str(build), "last_seen_at": time.time()})
            except (CryptoError, KeyError):
                pass
        latest = info["latest_build"]
        info["update_available"] = bool(latest and latest > build)
        info["update_required"] = bool(info["min_build"] and build < info["min_build"])
    return info
