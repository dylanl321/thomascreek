"""Frigate (events, snapshots, clips) and go2rtc (WebRTC signalling).

Video does not pass through the hub: the hub only relays the WebRTC offer and
answer, then the media flows between the phone and go2rtc directly. Frigate's
credential stays in the hub.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from ..config import FrigateConfig
from .base import Adapter, AdapterError


class Frigate(Adapter):
    name = "frigate"
    ping_path = "/api/version"
    title = "Cameras"
    cfg: FrigateConfig

    def __init__(self, cfg: FrigateConfig, vault):
        super().__init__(cfg, vault)
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._go2rtc: httpx.AsyncClient | None = None
        self._names: set[str] = set()
        self._names_at = 0.0
        self._info: dict[str, dict[str, Any]] = {}
        self._info_at = 0.0
        self.last_event: dict[str, dict[str, Any]] = {}

    async def _login(self) -> None:
        # Only needed on Frigate's authenticated port (8971). Port 5000 is open.
        if not self.cfg.username:
            return
        if self._jwt and time.time() - self._jwt_at < 6 * 3600:
            return
        password = self.secret(self.cfg.password_secret)
        resp = await self.client().post("/api/login", json={"user": self.cfg.username, "password": password})
        if resp.status_code != 200:
            self._fail("Frigate refused the hub's login")
            raise AdapterError(self.last_error)
        token = resp.cookies.get("frigate_token")
        if not token:
            for name, value in resp.cookies.items():
                token = value if "token" in name else token
        self._jwt, self._jwt_at = token, time.time()

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt}"} if self._jwt else {}

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        await self._login()
        return await super().request(method, path, **kw)

    async def cameras(self) -> list[str]:
        if self.cfg.cameras:
            return self.cfg.cameras
        cfg = await self.get_json("/api/config")
        return sorted(cfg.get("cameras", {}).keys())

    async def has_camera(self, camera: str) -> bool:
        """Whether Frigate has a camera of this name (Home Assistant may list others)."""
        if not self.enabled:
            return False
        if time.time() - self._names_at > 300:
            try:
                self._names = set(await self.cameras())
                self._names_at = time.time()
            except AdapterError:
                if not self._names:
                    return False
        return camera in self._names

    async def camera_info(self, max_age: float = 300) -> dict[str, dict[str, Any]]:
        """name -> {width, height, enabled, online}, from Frigate's config and stats."""
        if self._info and time.time() - self._info_at < max_age:
            return self._info
        cfg = await self.get_json("/api/config")
        try:
            stats = await self.get_json("/api/stats")
        except AdapterError:
            stats = {}
        out = {}
        for name, c in (cfg.get("cameras") or {}).items():
            det = c.get("detect") or {}
            st = (stats.get("cameras") or {}).get(name) or {}
            out[name] = {
                "width": det.get("width"), "height": det.get("height"),
                "enabled": c.get("enabled", True) is not False,
                "online": bool(st.get("camera_fps", 0)) if st else None,
                "name": (c.get("ui") or {}).get("name") or c.get("friendly_name"),
                "order": (c.get("ui") or {}).get("order", 0),
            }
        self._info, self._info_at = out, time.time()
        self._names, self._names_at = set(out), time.time()
        return out

    async def events(self, camera: str | None = None, limit: int = 25, before: float | None = None,
                     label: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "include_thumbnails": 0}
        if camera:
            params["cameras"] = camera
        if before:
            params["before"] = before
        if label:
            params["labels"] = label
        return await self.get_json("/api/events", params=params)

    async def event(self, event_id: str) -> dict[str, Any]:
        return await self.get_json(f"/api/events/{quote(event_id)}")

    async def mark_false_positive(self, event_id: str) -> None:
        """Send the event to Frigate+ as a false positive (needs Frigate+ and a snapshot)."""
        await self.request("PUT", f"/api/events/{quote(event_id)}/false_positive")

    async def event_snapshot(self, event_id: str) -> bytes:
        return (await self.request("GET", f"/api/events/{quote(event_id)}/snapshot.jpg", params={"bbox": 1})).content

    async def latest_jpg(self, camera: str, height: int = 360) -> bytes:
        return (await self.request("GET", f"/api/{quote(camera)}/latest.jpg", params={"h": height})).content

    async def event_thumb(self, event_id: str) -> bytes:
        return (await self.request("GET", f"/api/events/{quote(event_id)}/thumbnail.jpg")).content

    async def event_clip(self, event_id: str) -> httpx.Response:
        return await self.request("GET", f"/api/events/{quote(event_id)}/clip.mp4")

    async def stats(self) -> dict[str, Any]:
        return await self.get_json("/api/stats")

    async def webrtc_answer(self, camera: str, offer_sdp: str) -> str:
        if not self.cfg.go2rtc_url:
            raise AdapterError("go2rtc address is not set")
        if self._go2rtc is None:
            self._go2rtc = httpx.AsyncClient(base_url=self.cfg.go2rtc_url.rstrip("/"), timeout=10, verify=self.cfg.verify_tls)
        try:
            resp = await self._go2rtc.post(
                "/api/webrtc", params={"src": camera},
                json={"type": "offer", "sdp": offer_sdp},
            )
        except httpx.HTTPError as exc:
            raise AdapterError("cannot reach the camera server") from exc
        if resp.status_code >= 400:
            raise AdapterError(f"camera server answered {resp.status_code}")
        data = resp.json()
        return data.get("sdp", "")

    def _stream_client(self) -> httpx.AsyncClient | None:
        if not self.cfg.go2rtc_url:
            return None
        if getattr(self, "_stream", None) is None:
            self._stream = httpx.AsyncClient(
                base_url=self.cfg.go2rtc_url.rstrip("/"),
                timeout=httpx.Timeout(8.0, read=None), verify=self.cfg.verify_tls)
        return self._stream

    async def hls_playlist(self, camera: str) -> str | None:
        """go2rtc's HLS playlist, or None when it isn't there. The TV never sees this host."""
        client = self._stream_client()
        if client is None:
            return None
        try:
            resp = await client.get("/api/stream.m3u8", params={"src": camera})
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not resp.text.lstrip().startswith("#EXTM3U"):
            return None
        return resp.text

    async def open_go2rtc(self, path: str, params: dict[str, str] | None = None):
        """A go2rtc path (a playlist segment or MJPEG), streamed. None when the path isn't safe."""
        client = self._stream_client()
        if client is None or not path or "://" in path or path.startswith("//") or ".." in path:
            return None
        try:
            resp = await client.send(
                client.build_request("GET", "/" + path.split("?", 1)[0].lstrip("/"), params=params), stream=True)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            await resp.aclose()
            return None
        return resp

    async def close(self) -> None:
        await super().close()
        if self._go2rtc:
            await self._go2rtc.aclose()
            self._go2rtc = None
        stream = getattr(self, "_stream", None)
        if stream is not None:
            await stream.aclose()
            self._stream = None
