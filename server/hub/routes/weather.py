"""The Weather page: forecast, alerts, outlooks and radar for home, the phone's location or a trip."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/weather", tags=["weather"])


@router.get("")
async def weather(place: str = Query("home", max_length=40), lat: float | None = Query(None, ge=-90, le=90),
                  lon: float | None = Query(None, ge=-180, le=180),
                  c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """`place`: home, here (with the phone's lat/lon) or trip:<id>. "here" within 25 miles of the house is home.
    Children get the forecast, what to wear and warnings by name only (no radar, outlooks or alert text)."""
    if c.subject.role == "guest" and place != "home":
        place = "home"
    out = await h.forecasts.page(c.subject, place, lat, lon)
    if out is None:
        raise error(404, "no_place", "That place has no location yet")
    return out


@router.get("/alerts/{alert_id:path}")
async def alert(alert_id: str, place: str = Query("home", max_length=40), lat: float | None = None,
                lon: float | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One alert in full (opened from a push or the smart card). Looks at home first, then the given place."""
    if c.subject.role == "child":
        raise error(403, "forbidden", "Ask a grown-up")
    found = next((a for a in await h.forecasts.home_alerts() if a["id"] == alert_id), None)
    if not found and place != "home":
        page = await h.forecasts.page(c.subject, place, lat, lon) or {}
        found = next((a for a in page.get("alerts") or [] if a["id"] == alert_id), None)
    if not found:
        raise error(404, "expired", "This alert has ended")
    return found


@router.get("/radar/{provider}/{frame}/{z}/{x}/{y}.png")
async def radar_tile(provider: str, frame: str, z: int, x: int, y: int,
                     c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    if c.subject.role == "child":
        raise error(403, "forbidden", "Ask a grown-up")
    if provider not in ("nexrad", "dwd", "rainviewer"):
        raise error(404, "not_found")
    try:
        data = await h.forecasts.tile(provider, frame, z, x, y)
    except Exception:  # noqa: BLE001
        data = None
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
