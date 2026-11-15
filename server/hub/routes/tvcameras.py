"""The Cameras screen on the TV: every camera, a picture and live video, with the TV's own sign-in.

GET  /v1/tv/cameras                     [{id, name, room, door, snapshot_url, stream_url, stream}] door cameras first
GET  /v1/tv/cameras/{cam}/snapshot.jpg  the latest frame (Frigate, else Home Assistant)
GET  /v1/tv/cameras/{cam}/stream        HLS through go2rtc (lines point back here), else MJPEG
GET  /v1/tv/cameras/{cam}/media?src=    one go2rtc playlist or segment; `src` is a go2rtc path, never a URL

Grown-up TV profiles only: a kids' profile gets 403 and the TV hides the screen. The TV never sees go2rtc,
Frigate or the camera logins.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..adapters.base import AdapterError
from ..home_alerts import playlist_folder, rewrite_playlist
from ..state import Hub
from .deps import Caller, caller, error, hub
from .home_alerts import _relay

router = APIRouter(prefix="/v1/tv/cameras", tags=["tv"])

PLAYLIST = "application/vnd.apple.mpegurl"


def _see(c: Caller) -> None:
    s = c.subject
    if not s.tv or s.is_child_like:
        raise error(403, "forbidden", "Not available")


def _camera(h: Hub, cam: str) -> dict[str, Any]:
    for row in h.home_alerts._catalog():
        if row["id"] == cam:
            return row
    raise error(404, "not_found", "No such camera")


def _route(cam: str) -> str:
    return f"/v1/tv/cameras/{cam}/media"


@router.get("")
async def cameras(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _see(c)
    kind = "hls" if h.frigate.cfg.go2rtc_url else "mjpeg"
    rows = [{
        **row,
        "snapshot_url": f"/v1/tv/cameras/{row['id']}/snapshot.jpg",
        "stream_url": f"/v1/tv/cameras/{row['id']}/stream",
        "stream": kind,
    } for row in h.home_alerts._catalog()]
    return {"cameras": rows}


@router.get("/{cam}/snapshot.jpg")
async def snapshot(cam: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    _see(c)
    _camera(h, cam)
    try:
        if h.frigate.enabled:
            data = await h.frigate.latest_jpg(cam)
        else:
            data = await h.ha.camera_proxy(f"camera.{cam}")
    except AdapterError:
        raise error(404, "gone", "The camera didn't answer") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{cam}/stream")
async def stream(cam: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    _see(c)
    _camera(h, cam)
    playlist = await h.frigate.hls_playlist(cam) if h.frigate.cfg.go2rtc_url else None
    if playlist:
        body = rewrite_playlist(playlist, cam, "api/", route=_route(cam))
        return Response(body, media_type=PLAYLIST, headers={"Cache-Control": "no-store"})
    upstream = await h.frigate.open_go2rtc("api/stream.mjpeg", {"src": cam}) if h.frigate.cfg.go2rtc_url else None
    if upstream is None:
        try:
            upstream = await h.ha.camera_stream(f"camera.{cam}")
        except AdapterError:
            raise error(404, "gone", "The camera didn't answer") from None
    return _relay(upstream)


@router.get("/{cam}/media")
async def media(cam: str, src: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    _see(c)
    _camera(h, cam)
    upstream = await h.frigate.open_go2rtc(src)
    if upstream is None:
        raise error(404, "gone", "That part of the stream has passed")
    if src.split("?", 1)[0].endswith(".m3u8"):
        try:
            text = (await upstream.aread()).decode("utf-8", "replace")
        finally:
            await upstream.aclose()
        return Response(rewrite_playlist(text, cam, playlist_folder(src), route=_route(cam)),
                        media_type=PLAYLIST, headers={"Cache-Control": "no-store"})
    return _relay(upstream)
