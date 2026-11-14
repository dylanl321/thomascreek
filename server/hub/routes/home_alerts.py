"""Door alerts for the TV. The picture and the stream are proxied with the caller's bearer."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..adapters.base import AdapterError
from ..home_alerts import rewrite_playlist
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/tv/alerts", tags=["tv"])


def _see(h: Hub, c: Caller) -> None:
    """A TV at home, anyone at home, or a phone that may already view cameras."""
    if c.subject.tv or (c.ip and h.is_home(c.ip)) or h.policy.has(c.subject, "cameras"):
        if c.subject.role == "pending":
            raise error(403, "forbidden", "Not available")
        return
    raise error(403, "forbidden", "Not available")


def _alert(h: Hub, alert_id: str) -> dict[str, Any]:
    row = h.home_alerts.get(alert_id)
    if row is None:
        raise error(404, "gone", "That alert has passed")
    return row


@router.get("/settings")
async def get_settings(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _see(h, c)
    return h.home_alerts.view()


@router.put("/settings")
async def put_settings(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only Dylan can change this")
    return h.home_alerts.set_settings(body)


@router.get("/{alert_id}/snapshot.jpg")
async def snapshot(alert_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    _see(h, c)
    row = _alert(h, alert_id)
    camera = row.get("camera")
    if not camera:
        raise error(404, "gone", "That picture has passed")
    try:
        if h.frigate.enabled:
            data = await h.frigate.latest_jpg(camera)
        else:
            data = await h.ha.camera_proxy(f"camera.{camera}")
    except AdapterError:
        raise error(404, "gone", "The camera didn't answer") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=5"})


@router.get("/{alert_id}/stream")
async def stream(alert_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """HLS when go2rtc is set, otherwise Home Assistant's MJPEG. Either way the TV uses its bearer."""
    _see(h, c)
    row = _alert(h, alert_id)
    camera = row.get("camera")
    if not camera:
        raise error(404, "gone", "That picture has passed")
    playlist = await h.frigate.hls_playlist(camera) if h.frigate.cfg.go2rtc_url else None
    if playlist:
        body = rewrite_playlist(playlist, row["id"])
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})
    upstream = await h.frigate.open_go2rtc("api/stream.mjpeg", {"src": camera}) if h.frigate.cfg.go2rtc_url else None
    if upstream is None:
        try:
            upstream = await h.ha.camera_stream(f"camera.{camera}")
        except AdapterError:
            raise error(404, "gone", "The camera didn't answer") from None
    return _relay(upstream)


@router.get("/{alert_id}/media")
async def media(alert_id: str, src: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """One HLS segment. `src` is a path on go2rtc, never a full URL."""
    _see(h, c)
    _alert(h, alert_id)
    upstream = await h.frigate.open_go2rtc(src)
    if upstream is None:
        raise error(404, "gone", "That part of the stream has passed")
    return _relay(upstream)


def _relay(upstream: Any) -> StreamingResponse:
    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    media_type = upstream.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(body(), media_type=media_type, headers={"Cache-Control": "no-store"})
