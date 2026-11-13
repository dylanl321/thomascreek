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

# For each media_player: the remote.* entity on the same device (if any) and the
# integration that made the device, so the hub knows which buttons it can press.
MEDIA_LINKS_TEMPLATE = """
{%- set ns = namespace(out=[]) -%}
{%- for mp in states.media_player -%}
  {%- set d = device_id(mp.entity_id) -%}
  {%- set r = (device_entities(d) | select('match', 'remote\\.') | list | first) if d else none -%}
  {%- set ids = (device_attr(d, 'identifiers') | list) if d else [] -%}
  {%- set ns.out = ns.out + [{'entity_id': mp.entity_id, 'remote': r, 'platform': (ids[0][0] if ids else none)}] -%}
{%- endfor -%}
{{ ns.out | tojson }}
"""

# Every Home Assistant device with its maker, model and integration, plus the integration behind each
# media player. The hub uses it to keep one entry per real TV and to group appliance entities.
MEDIA_INTEGRATIONS = (
    "androidtv_remote", "apple_tv", "cast", "roku", "webostv", "samsungtv", "vizio", "braviatv", "androidtv",
    "firetv", "xbox", "ps4", "playstation_network", "dlna_dmr", "plex", "sonos", "music_assistant", "esphome",
    "wyoming", "fully_kiosk", "browser_mod", "spotify", "squeezebox", "heos", "denonavr", "linkplay",
    "mobile_app", "jellyfin", "emby", "kodi", "bluesound", "yamaha_musiccast", "airplay", "upnp",
)
DEVICES_TEMPLATE = """
{%- set ns = namespace(dev=[], plat=[]) -%}
{%- for dom in %(doms)s -%}
  {%- for e in integration_entities(dom) | select('match', 'media_player\\.') -%}
    {%- set ns.plat = ns.plat + [[e, dom]] -%}
  {%- endfor -%}
{%- endfor -%}
{%- for d in states | map(attribute='entity_id') | map('device_id') | reject('none') | unique -%}
  {%- set ids = device_attr(d, 'identifiers') | list -%}
  {%- set ns.dev = ns.dev + [{'id': d, 'name': device_attr(d, 'name_by_user') or device_attr(d, 'name'),
      'manufacturer': device_attr(d, 'manufacturer'), 'model': device_attr(d, 'model'),
      'integration': (ids[0][0] if ids else none), 'entities': device_entities(d)}] -%}
{%- endfor -%}
{{ {'devices': ns.dev, 'platforms': ns.plat} | tojson }}
""".replace("%(doms)s", "[" + ", ".join(f"'{d}'" for d in MEDIA_INTEGRATIONS) + "]")

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
    ping_path = "/api/"
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

    async def media_links(self) -> list[dict[str, Any]]:
        try:
            return json.loads(await self.render(MEDIA_LINKS_TEMPLATE))
        except Exception:  # noqa: BLE001
            return []

    async def devices(self) -> dict[str, Any]:
        """Device registry facts ({devices, platforms}); empty if the template engine can't give them."""
        try:
            out = json.loads(await self.render(DEVICES_TEMPLATE))
            return out if isinstance(out, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def floors(self) -> list[dict[str, Any]]:
        try:
            return json.loads(await self.render(FLOORS_TEMPLATE))
        except Exception:  # floors() needs HA 2024.4+
            return []

    async def logbook(self, start_iso: str, end_iso: str | None = None,
                      entities: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if end_iso:
            params["end_time"] = end_iso
        if entities:
            # Only the entities asked for: the whole house's logbook is large and slow.
            params["entity"] = ",".join(entities)
        return await self.get_json(f"/api/logbook/{start_iso}", params=params or None)

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
