"""Cameras: snapshots and events through the hub, live video over WebRTC.

The app asks for a pass (secure tier when away from home, so Face ID), then
loads `player_url` in a web view. That small page sends its WebRTC offer to the
hub, the hub relays it to go2rtc, and video flows directly from go2rtc to the
phone. No camera credential ever reaches the phone.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..crypto import CryptoError
from ..home import iso
from ..policy import Target
from ..state import Hub
from .deps import Caller, caller, error, hub, need, require_confirm

router = APIRouter(prefix="/v1/cameras", tags=["cameras"])

PASS_AUD = "camera-pass"
PASS_TTL_S = 300


def _camera_entity(h: Hub, cam: str) -> str:
    if not cam.replace("_", "").isalnum():
        raise error(404, "not_found", "No such camera")
    # With Frigate set up, only Frigate's cameras exist for the app.
    if h.frigate.enabled and h.frigate._names and cam not in h.frigate._names:
        raise error(404, "not_found", "No such camera")
    return f"camera.{cam}"


def camera_name(h: Hub, cam: str) -> str:
    info = h.frigate._info.get(cam) or {}
    if info.get("name"):
        return info["name"]
    ha = (h.home.states.get(f"camera.{cam}") or {}).get("attributes", {}).get("friendly_name")
    if ha:
        return ha
    words = [w for w in cam.split("_") if not w.isdigit()] or [cam]
    return " ".join(w.capitalize() for w in words)


def _cursor(rows: list[dict[str, Any]], limit: int) -> str | None:
    """The next page's `before`, as a string (the app's contract)."""
    if len(rows) < limit or rows[-1].get("start_time") is None:
        return None
    return repr(float(rows[-1]["start_time"]))


def event_item(h: Hub, e: dict[str, Any]) -> dict[str, Any]:
    """One Frigate event in the app's shape."""
    data = e.get("data") or {}
    eid = e["id"]
    return {
        "id": eid, "camera": e.get("camera"), "camera_name": camera_name(h, e.get("camera") or ""),
        "label": e.get("label"), "sub_label": e.get("sub_label") if isinstance(e.get("sub_label"), str) else
        ((e.get("sub_label") or [None])[0] if isinstance(e.get("sub_label"), list) else None),
        "score": data.get("top_score") or e.get("top_score") or data.get("score"),
        "zones": e.get("zones") or [], "start": iso(e.get("start_time")), "end": iso(e.get("end_time")),
        "thumb_url": f"/v1/cameras/events/{eid}/thumb.jpg",
        "snapshot_url": f"/v1/cameras/events/{eid}/snapshot.jpg" if e.get("has_snapshot") else None,
        "clip_url": f"/v1/cameras/events/{eid}/clip.mp4" if e.get("has_clip") else None,
    }


def camera_list(h: Hub) -> list[dict[str, Any]]:
    """Frigate's cameras with their real frame size, for /v1/cameras and /v1/home."""
    out = []
    for cam, info in sorted(h.frigate._info.items(), key=lambda kv: (kv[1].get("order") or 0, kv[0])):
        if not info.get("enabled", True):
            continue
        last = h.frigate.last_event.get(cam)
        out.append({
            "id": cam, "name": camera_name(h, cam), "width": info.get("width"), "height": info.get("height"),
            "online": info.get("online"), "room_id": h.home.entity_area.get(f"camera.{cam}"),
            "snapshot_url": f"/v1/cameras/{cam}/snapshot.jpg",
            "last_event": {"label": last.get("label"), "start": last.get("start")} if last else None,
        })
    return out


@router.get("")
async def cameras(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "cameras")
    if not h.frigate.enabled:
        return {"cameras": []}
    try:
        await h.frigate.camera_info()
    except AdapterError as exc:
        if not h.frigate._info:
            raise error(503, "unavailable", str(exc)) from None
    return {"cameras": camera_list(h)}


@router.get("/events")
async def all_events(limit: int = 30, before: float | None = None, camera: str | None = None,
                     label: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Every camera's events, newest first. `before` is the previous page's next_before."""
    need(h, c, "cameras")
    if camera:
        _camera_entity(h, camera)
    limit = max(1, min(limit, 100))
    try:
        rows = await h.frigate.events(camera, limit, before=before, label=label)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    return {"items": [event_item(h, e) for e in rows],
            "next_before": _cursor(rows, limit)}


@router.get("/events/{event_id}")
async def one_event(event_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "cameras")
    try:
        return event_item(h, await h.frigate.event(event_id))
    except AdapterError:
        raise error(404, "not_found", "No such event") from None


@router.get("/events/{event_id}/snapshot.jpg")
async def event_snapshot(event_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    need(h, c, "cameras")
    try:
        return Response(await h.frigate.event_snapshot(event_id), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    except AdapterError:
        raise error(404, "not_found", "No snapshot") from None


@router.post("/{cam}/pass")
async def camera_pass(cam: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    eid = _camera_entity(h, cam)
    d = h.policy.decide(c.subject, "camera.view", Target(entity_id=eid, area=h.home.entity_area.get(eid), kind="camera"))
    if not d.allowed:
        raise error(403, "forbidden", "Not available", ask_parent=False)
    await require_confirm(request, h, c, d.tier)
    token, exp = h.tokens.issue_scoped({"aud": PASS_AUD, "cam": cam, "dev": c.device["id"]}, PASS_TTL_S)
    h.db.log("camera.view", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=eid,
             params={"away": not c.subject.at_home})
    return {"player_url": f"/v1/cameras/{cam}/player?pass={token}", "expires_at": iso(exp)}


def _check_pass(h: Hub, cam: str, token: str) -> dict[str, Any]:
    try:
        claims = h.tokens.verify(token, audience=PASS_AUD)
    except CryptoError:
        raise error(401, "expired", "Camera pass expired") from None
    if claims.get("cam") != cam:
        raise error(403, "forbidden", "Pass is for another camera")
    dev = h.db.device(claims["dev"])
    if dev is None or dev["status"] != "active":
        raise error(401, "revoked", "Device removed")
    return claims


PLAYER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title><style>html,body{{margin:0;height:100%;background:#000}}
video,img{{width:100%;height:100%;object-fit:contain}}#m{{position:fixed;top:45%;width:100%;text-align:center;
color:#bbb;font:15px -apple-system,system-ui}}</style></head><body>
<video id="v" autoplay playsinline muted></video><div id="m">Connecting…</div>
<script>
(async () => {{
  const m = document.getElementById('m'), v = document.getElementById('v');
  const base = location.pathname.replace(/player$/, '');
  function mjpeg() {{
    // Fallback: Home Assistant's MJPEG stream, relayed by the hub.
    const i = document.createElement('img');
    i.onload = () => {{ m.remove(); v.remove(); }};
    i.onerror = () => {{ m.textContent = 'Camera unavailable'; }};
    i.src = base + 'mjpeg' + location.search;
    document.body.appendChild(i);
  }}
  if (!{webrtc}) return mjpeg();
  try {{
    const pc = new RTCPeerConnection();
    pc.addTransceiver('video', {{direction: 'recvonly'}});
    pc.addTransceiver('audio', {{direction: 'recvonly'}});
    pc.ontrack = e => {{ v.srcObject = e.streams[0]; m.remove(); }};
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(r => {{ if (pc.iceGatheringState === 'complete') r(); pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && r(); setTimeout(r, 1500); }});
    const res = await fetch(base + 'webrtc' + location.search,
      {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{sdp: pc.localDescription.sdp}})}});
    if (!res.ok) throw new Error('camera unavailable');
    await pc.setRemoteDescription({{type: 'answer', sdp: (await res.json()).sdp}});
  }} catch (e) {{ mjpeg(); }}
}})();
</script></body></html>"""


@router.get("/{cam}/player", response_class=HTMLResponse)
async def player_page(cam: str, request: Request, h: Hub = Depends(hub)) -> HTMLResponse:
    _check_pass(h, cam, request.query_params.get("pass", ""))
    return HTMLResponse(PLAYER_HTML.format(title=html.escape(cam), webrtc="true" if h.frigate.enabled else "false"),
                        headers={"Cache-Control": "no-store", "Content-Security-Policy":
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; img-src 'self'; media-src * blob:"})


class Offer(BaseModel):
    sdp: str = Field(max_length=20000)


@router.get("/{cam}/mjpeg")
async def mjpeg(cam: str, request: Request, h: Hub = Depends(hub)) -> StreamingResponse:
    _check_pass(h, cam, request.query_params.get("pass", ""))
    eid = _camera_entity(h, cam)
    try:
        upstream = await h.ha.camera_stream(eid)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
                             headers={"Cache-Control": "no-store"})


@router.post("/{cam}/webrtc")
async def webrtc(cam: str, body: Offer, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    _check_pass(h, cam, request.query_params.get("pass", ""))
    try:
        answer = await h.frigate.webrtc_answer(cam, body.sdp)
    except AdapterError as exc:
        raise error(502, "unavailable", str(exc)) from None
    return {"type": "answer", "sdp": answer}


@router.get("/{cam}/snapshot.jpg")
async def snapshot(cam: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    need(h, c, "cameras")
    eid = _camera_entity(h, cam)
    try:
        data = await h.frigate.latest_jpg(cam) if h.frigate.enabled else await h.ha.camera_proxy(eid)
    except AdapterError:
        raise error(503, "unavailable", "Camera unavailable") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=10"})


@router.get("/{cam}/events")
async def events(cam: str, limit: int = 25, before: float | None = None, c: Caller = Depends(caller),
                 h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "cameras")
    _camera_entity(h, cam)
    limit = max(1, min(limit, 100))
    try:
        rows = await h.frigate.events(cam, limit, before=before)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    return {"items": [event_item(h, e) for e in rows],
            "next_before": _cursor(rows, limit)}


@router.get("/events/{event_id}/thumb.jpg")
async def event_thumb(event_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    need(h, c, "cameras")
    try:
        return Response(await h.frigate.event_thumb(event_id), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    except AdapterError:
        raise error(404, "not_found", "No thumbnail") from None


@router.get("/events/{event_id}/clip.mp4")
async def event_clip(event_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    need(h, c, "cameras")
    try:
        resp = await h.frigate.event_clip(event_id)
    except AdapterError:
        raise error(404, "not_found", "No clip") from None
    return Response(resp.content, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})
