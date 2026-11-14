"""The shared Home Assistant websocket: states, registries and service calls on one connection, REST as the
fallback, and slow lights answered "accepted" with the outcome sent later."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
import websockets

from hub.adapters.base import ServiceError
from hub.ha_ws import HAConnection, NotConnected, areas_from, devices_from, floors_from, media_links_from

REG = {
    "area": [{"area_id": "kitchen", "name": "Kitchen", "floor_id": "main"},
             {"area_id": "den", "name": "Den", "floor_id": None}],
    "floor": [{"floor_id": "main", "name": "Main Floor"}],
    "device": [{"id": "d1", "name": "Hue bulb", "name_by_user": None, "manufacturer": "Signify", "model": "A19",
                "identifiers": [["hue", "x"]], "area_id": "kitchen"},
               {"id": "tv", "name": "Shield", "manufacturer": "NVIDIA", "model": "Shield",
                "identifiers": [["androidtv_remote", "y"]], "area_id": "den"}],
    "entity": [{"entity_id": "light.kitchen", "device_id": "d1", "area_id": None, "platform": "hue"},
               {"entity_id": "light.living_room", "device_id": None, "area_id": "den", "platform": "hue"},
               {"entity_id": "media_player.theo_tv", "device_id": "tv", "area_id": None, "platform": "androidtv_remote"},
               {"entity_id": "remote.theo_tv", "device_id": "tv", "area_id": None, "platform": "androidtv_remote"}],
}


def test_registries_give_the_same_shapes_as_the_templates():
    assert areas_from(REG) == [
        {"id": "kitchen", "name": "Kitchen", "floor": "main", "entities": ["light.kitchen"]},
        {"id": "den", "name": "Den", "floor": None,
         "entities": ["light.living_room", "media_player.theo_tv", "remote.theo_tv"]}]
    assert floors_from(REG) == [{"id": "main", "name": "Main Floor"}]
    devs = devices_from(REG, ("androidtv_remote",), {"light.kitchen", "media_player.theo_tv"})
    assert [d["id"] for d in devs["devices"]] == ["d1", "tv"] and devs["devices"][0]["integration"] == "hue"
    assert devs["platforms"] == [["media_player.theo_tv", "androidtv_remote"]]
    assert media_links_from(REG, {"media_player.theo_tv": {}}) == [
        {"entity_id": "media_player.theo_tv", "remote": "remote.theo_tv", "platform": "androidtv_remote"}]


class FakeHA:
    """Just enough of Home Assistant's websocket API."""

    def __init__(self, states):
        self.states = states
        self.calls: list[dict] = []
        self.clients: list = []
        self.fail_next: dict | None = None
        self.slow_s = 0.0

    async def handler(self, ws):
        self.clients.append(ws)
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "auth_ok" if auth.get("access_token") == "tok" else "auth_invalid"}))
        async for raw in ws:
            msg = json.loads(raw)
            t, mid = msg["type"], msg["id"]
            if t == "ping":
                await ws.send(json.dumps({"id": mid, "type": "pong"}))
                continue
            result: object = None
            if t == "get_states":
                result = self.states
            elif t.startswith("config/"):
                result = REG[t.split("/")[1].split("_")[0]]
            elif t == "call_service":
                self.calls.append(msg)
                if self.slow_s:
                    await asyncio.sleep(self.slow_s)
                if self.fail_next:
                    err, self.fail_next = self.fail_next, None
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": False, "error": err}))
                    continue
                result = {"context": {"id": "c"}}
            await ws.send(json.dumps({"id": mid, "type": "result", "success": True, "result": result}))

    async def push_state(self, new):
        ev = {"type": "event", "event": {"event_type": "state_changed",
                                         "data": {"entity_id": new["entity_id"], "old_state": None, "new_state": new}}}
        for ws in self.clients:
            await ws.send(json.dumps({"id": 1, **ev}))


@pytest.fixture
async def live(hub):
    from tests.conftest import STATES

    fake = FakeHA([dict(s) for s in STATES])
    server = await websockets.serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hub.config.home_assistant.url = f"http://127.0.0.1:{port}"
    hub.vault.set(hub.config.home_assistant.token_secret, "tok")
    conn = HAConnection(hub)
    hub.ha_ws = conn
    hub.ha.ws = conn
    hub.home.PUBLISH_DEBOUNCE_S = 0.01
    conn.start()
    for _ in range(100):
        if conn.ready:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    yield hub, fake, conn
    await conn.stop()
    server.close()
    await server.wait_closed()


async def test_state_changes_reach_phones_and_alerts_at_once(live):
    hub, fake, conn = live
    assert conn.ready
    seen, alerts = [], []
    hub.bus.publish = lambda e, *a, **kw: seen.append(e)  # type: ignore[method-assign]

    async def on_state(eid, old, new, now=None):
        alerts.append(eid)
        return 0
    hub.alerts.on_state = on_state  # type: ignore[method-assign]
    await fake.push_state({"entity_id": "light.living_room", "state": "on", "attributes": {"brightness": 200},
                           "last_updated": "2026-11-14T13:00:00+00:00"})
    await asyncio.sleep(0.15)
    assert hub.home.states["light.living_room"]["state"] == "on"
    assert {"type": "device", "entity_id": "light.living_room"} in seen
    assert alerts == ["light.living_room"]


async def test_service_calls_go_over_the_websocket(live):
    hub, fake, conn = live
    from hub.adapters.homeassistant import HomeAssistant

    ha = HomeAssistant(hub.config.home_assistant, hub.vault)
    ha.ws = conn
    await ha.call_service("light", "turn_on", {"entity_id": ["light.kitchen", "light.den"], "brightness_pct": 40})
    assert fake.calls[-1]["target"] == {"entity_id": ["light.kitchen", "light.den"]}
    assert fake.calls[-1]["service_data"] == {"brightness_pct": 40}
    fake.fail_next = {"code": "home_assistant_error", "message": "Zigbee timeout"}
    with pytest.raises(ServiceError) as err:
        await ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert err.value.status == 500 and err.value.reason == "Zigbee timeout"
    await ha.close()


async def test_rooms_come_from_the_registries(live):
    hub, fake, conn = live
    await hub.home._refresh_areas()
    assert hub.home.entity_area["light.kitchen"] == "kitchen"
    assert hub.home.media_links["media_player.theo_tv"]["remote"] == "remote.theo_tv"


async def test_rest_poll_is_only_a_safety_net_while_connected(live):
    hub, fake, conn = live
    reads = []

    async def states():
        reads.append(1)
        return list(hub.home.states.values())
    hub.ha.states = states  # type: ignore[method-assign]
    hub.home.areas_at = time.time()
    await hub.home.refresh()
    assert reads == []
    await conn.stop()
    await hub.home.refresh()
    assert reads == [1]


@respx.mock
async def test_rest_takes_over_when_the_websocket_is_down(hub):
    hub.vault.set(hub.config.home_assistant.token_secret, "t")
    from hub.adapters.homeassistant import HomeAssistant

    conn = HAConnection(hub)
    ha = HomeAssistant(hub.config.home_assistant, hub.vault)
    ha.ws = conn
    route = respx.post("http://ha.test/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))
    assert await ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"}) == []
    assert route.called
    with pytest.raises(NotConnected):
        await conn.request({"type": "ping"})
    await ha.close()


def test_a_slow_light_is_accepted_and_its_failure_reported(hub, people, home_client):
    hub.config.hub.dry_run = False
    hub.config.hub.accept_after_s = 0.05
    dylan = people["phones"]["Dylan"]
    seen = []
    hub.bus.publish = lambda e, *a, **kw: seen.append((e, kw))  # type: ignore[method-assign]

    async def slow_fail(domain, service, data):
        await asyncio.sleep(0.2)
        raise ServiceError("x", 500, "Bulb unreachable")
    hub.ha.call_service = slow_fail
    headers = {**dylan.h, "X-TC-Request-Id": "req-slow-0001", "X-TC-Accept": "202"}
    r = home_client.post("/v1/actions", headers=headers,
                         json={"action": "light.set", "target": "light.living_room", "params": {"on": True}})
    assert r.status_code == 202 and r.json()["accepted"] is True
    end = time.time() + 2
    while not seen and time.time() < end:
        time.sleep(0.02)
    ev, kw = seen[0]
    assert ev["type"] == "action_result" and ev["data"]["request_id"] == "req-slow-0001"
    assert ev["data"]["ok"] is False and "Bulb unreachable" in ev["data"]["message"]
    assert kw["device_id"] == dylan.device_id

    # without the opt-in header (an older app) it waits as before
    seen.clear()
    r = home_client.post("/v1/actions", headers={**dylan.h, "X-TC-Request-Id": "req-slow-0002"},
                         json={"action": "light.set", "target": "light.living_room", "params": {"on": True}})
    assert r.status_code == 502


def test_a_fast_light_still_gets_a_normal_answer(hub, people, home_client):
    hub.config.hub.dry_run = False
    dylan = people["phones"]["Dylan"]
    headers = {**dylan.h, "X-TC-Request-Id": "req-fast-0001", "X-TC-Accept": "202"}
    r = home_client.post("/v1/actions/batch", headers=headers, json={"action": "light.set", "items": [
        {"target": "light.living_room", "params": {"on": True}}, {"target": "light.kitchen", "params": {"on": True}}]})
    assert r.status_code == 200 and r.json()["message"] == "Done" and len(r.json()["targets"]) == 2
