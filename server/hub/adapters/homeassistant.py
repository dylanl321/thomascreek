"""Home Assistant over its REST API.

Reads: /api/states, plus areas and floors through /api/template (the REST API
has no area registry endpoint, but the template engine exposes areas(),
area_entities(), floor_id() and friends).

Writes go through call_service, and the hub only calls it when dry-run is off
for that action (see hub.actions).
"""

from __future__ import annotations

import json
from typing import Any

from .base import Adapter, AdapterError

AREAS_TEMPLATE = """
{%- set ns = namespace(out=[]) -%}
{%- for a in areas() -%}
  {%- set e = namespace(l=area_entities(a)) -%}
  {%- for d in area_devices(a) -%}{%- set e.l = e.l + device_entities(d) -%}{%- endfor -%}
  {%- set ns.out = ns.out + [{"id": a, "name": area_name(a), "floor": floor_id(a), "entities": e.l | unique | list}] -%}
{%- endfor -%}
{{ ns.out | tojson }}
"""

FLOORS_TEMPLATE = """
{%- set ns = namespace(out=[]) -%}
{%- for f in floors() -%}
  {%- set ns.out = ns.out + [{"id": f, "name": floor_name(f)}] -%}
{%- endfor -%}
{{ ns.out | tojson }}
"""


class HomeAssistant(Adapter):
    name = "home_assistant"
    title = "Home Assistant"

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def states(self) -> list[dict[str, Any]]:
        return await self.get_json("/api/states")

    async def render(self, template: str) -> str:
        resp = await self.request("POST", "/api/template", json={"template": template})
        return resp.text

    async def areas(self) -> list[dict[str, Any]]:
        return json.loads(await self.render(AREAS_TEMPLATE))

    async def floors(self) -> list[dict[str, Any]]:
        try:
            return json.loads(await self.render(FLOORS_TEMPLATE))
        except Exception:  # floors() needs HA 2024.4+
            return []

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return await self.post_json(f"/api/services/{domain}/{service}", data)

    async def camera_proxy(self, entity_id: str) -> bytes:
        return (await self.request("GET", f"/api/camera_proxy/{entity_id}")).content

    async def camera_stream(self, entity_id: str):
        """Open HA's MJPEG stream for a camera (caller must close the response)."""
        import httpx

        req = self.client().build_request("GET", f"/api/camera_proxy_stream/{entity_id}",
                                          headers=self.auth_headers(),
                                          timeout=httpx.Timeout(10.0, read=None))
        try:
            resp = await self.client().send(req, stream=True)
        except httpx.HTTPError as exc:
            raise AdapterError(f"cannot reach Home Assistant: {type(exc).__name__}") from None
        if resp.status_code != 200:
            await resp.aclose()
            raise AdapterError(f"Home Assistant camera stream: HTTP {resp.status_code}")
        return resp
