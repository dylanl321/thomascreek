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
    title = "Cameras"
    cfg: FrigateConfig

    def __init__(self, cfg: FrigateConfig, vault):
        super().__init__(cfg, vault)
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._go2rtc: httpx.AsyncClient | None = None

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

    async def events(self, camera: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "include_thumbnails": 0}
        if camera:
            params["cameras"] = camera
        return await self.get_json("/api/events", params=params)

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

    async def close(self) -> None:
        await super().close()
        if self._go2rtc:
            await self._go2rtc.aclose()
            self._go2rtc = None
