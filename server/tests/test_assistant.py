"""Ask Thomas Creek: tools run as the caller, Face ID and deletes become confirm cards."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


class FakeModel:
    """Plays back a script: each step is a list of tool calls, or a final text."""

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> Any:
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        step = self.script.pop(0)
        if isinstance(step, str):
            return SimpleNamespace(content=[{"type": "text", "text": step}], stop_reason="end_turn", usage=None)
        blocks = [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(step)]
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=None)

    def results(self, i: int) -> list[dict[str, Any]]:
        msgs = self.requests[i]["messages"]
        return [json.loads(b["content"]) for b in msgs[-1]["content"]]


@pytest.fixture
def ai(hub):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    hub.config.hub.dry_run = False

    def use(script):
        fake = FakeModel(script)
        hub.assistant.create = fake
        return fake
    return use


def ask(client, phone, text, **kw):
    return client.post("/v1/assistant", json={"text": text, **kw}, headers=phone.h)


def test_needs_a_key(hub, home_client, people):
    r = ask(home_client, people["phones"]["Avery"], "lights off")
    assert r.status_code == 503 and "admin page" in r.json()["message"]
    assert home_client.get("/v1/assistant", headers=people["phones"]["Avery"].h).json() == {"ready": False}


def test_adult_turns_off_a_light(hub, home_client, people, ai):
    fake = ai([
        [("house", {"query": "kitchen"})],
        [("act", {"action": "light.set", "target": "light.kitchen", "params": {"on": False}, "summary": "Kitchen off"})],
        "Kitchen light is off.",
    ])
    r = ask(home_client, people["phones"]["Avery"], "turn off the kitchen light", screen="Home")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Kitchen light is off." and body["changed"] is True
    assert body["steps"] == [{"label": "Kitchen off", "status": "done"}]
    assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in hub.calls
    house = fake.results(1)[0]
    assert [d["id"] for d in house["devices"]] == ["light.kitchen"]
    # Who is asking rides in a system message after the question, not the cached system prompt
    sysmsg = fake.requests[0]["messages"][1]
    assert sysmsg["role"] == "system" and "Avery" in sysmsg["content"] and "Home" in sysmsg["content"]
    assert "Avery" not in json.dumps(fake.requests[0]["system"])
    row = hub.db.execute("SELECT source FROM activity WHERE action='light.set' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row[0] == "ask"
    # The conversation continues
    ai(["Sure."])
    r2 = ask(home_client, people["phones"]["Avery"], "thanks", conversation_id=body["conversation_id"])
    assert r2.json()["conversation_id"] == body["conversation_id"]


def test_garage_needs_face_id_card(hub, home_client, people, ai):
    fake = ai([
        [("act", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True},
                  "summary": "Open the large garage door"})],
        "Confirm in the card and it'll open.",
    ])
    body = ask(home_client, people["phones"]["Dylan"], "open the garage").json()
    assert hub.calls == []
    assert fake.results(1)[0]["waiting"] is True
    (card,) = body["confirms"]
    assert card["kind"] == "faceid" and card["method"] == "POST" and card["path"] == "/v1/actions"
    assert card["body"] == {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}}
    # The app replays it with Face ID, then tells the assistant
    phone = people["phones"]["Dylan"]
    assert phone.secure_post(card["path"], card["body"]).status_code == 200
    assert ("cover", "open_cover", {"entity_id": "cover.large_garage"}) in hub.calls
    fake2 = ai(["It's open."])
    ask(home_client, phone, "is it open?", conversation_id=body["conversation_id"],
        confirmed=[{"id": card["id"], "ok": True}])
    assert "Open the large garage door: done" in fake2.requests[0]["messages"][-2]["content"]


def test_child_is_refused_and_sees_less(hub, home_client, people, ai):
    fake = ai([
        [("house", {}), ("act", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True},
                                 "summary": "Open garage"}),
         ("hub_get", {"path": "/v1/cameras"})],
        "Garages are for grown-ups.",
    ])
    body = ask(home_client, people["phones"]["Theo"], "open the garage and show cameras").json()
    house, act, cams = fake.results(1)
    ids = {d["id"] for d in house["devices"]}
    assert "cover.large_garage" not in ids and "camera.driveway" not in ids and "light.theo_room" in ids
    assert act["error"] and cams["error"]
    assert hub.calls == [] and body["confirms"] == []
    assert body["steps"] == [{"label": "Open garage", "status": "denied"}]
    tools = {t["name"]: t for t in fake.requests[0]["tools"]}
    assert "/v1/cameras" not in tools["hub_get"]["description"]
    assert "/v1/chores" in tools["hub_get"]["description"]


def test_deletes_wait_and_private_routes_are_closed(hub, home_client, people, ai):
    fake = ai([
        [("hub_change", {"method": "DELETE", "path": "/v1/meals/recipes/tacos", "summary": "Delete Tacos"}),
         ("hub_get", {"path": "/v1/vpn/pool"}), ("hub_get", {"path": "/v1/admin/web-link"}),
         ("hub_get", {"path": "v1/chores"})],
        "Tap confirm to delete it.",
    ])
    body = ask(home_client, people["phones"]["Dylan"], "delete the tacos recipe").json()
    dele, vpn, admin, chores = fake.results(1)
    assert dele["waiting"] and body["confirms"][0]["kind"] == "tap"
    assert "isn't available" in vpn["error"] and "isn't available" in admin["error"]
    assert "error" not in chores or "403" not in chores.get("error", "")
    desc = next(t for t in fake.requests[0]["tools"] if t["name"] == "hub_get")["description"]
    assert "/v1/vpn" not in desc and "/v1/admin" not in desc and "/v1/auth" not in desc


def test_big_results_are_trimmed_whole_never_cut_mid_json():
    from hub.assistant import _fit
    out = {"devices": [{"id": f"light.l{i}", "name": f"Light {i}", "state": {"on": True}} for i in range(2000)],
           "status": "ok"}
    text = _fit(out, limit=5000)
    j = json.loads(text)  # still valid JSON
    assert len(text) <= 5000 and j["status"] == "ok"
    assert 0 < len(j["devices"]) < 2000 and j["more"].startswith(f"{2000 - len(j['devices'])} more devices")
    small = {"devices": [1, 2]}
    assert json.loads(_fit(small)) == small


def test_house_active_lists_only_what_is_on(hub, home_client, people, ai):
    fake = ai([[("house", {"kind": "light", "active": True})], "Done."])
    ask(home_client, people["phones"]["Avery"], "which lights are on?")
    res = fake.results(1)[0]
    assert res["devices"] and all(d["state"].get("on") for d in res["devices"])
    assert "actions_by_kind" in res and all("actions" not in d for d in res["devices"])


def _add(hub, eid, state, area=None, **attrs):
    hub.home.states[eid] = {"entity_id": eid, "state": state, "attributes": attrs,
                            "last_updated": "2026-11-12T01:00:00+00:00", "last_changed": "2026-11-12T01:00:00+00:00"}
    if area:
        hub.home.entity_area[eid] = area


def test_sprinkler_zones_are_timed_valves(hub, home_client, people):
    from hub.home import kind_for
    hub.config.hub.dry_run = False
    _add(hub, "switch.irrigation_controller_garden", "off", "kitchen", friendly_name="Irrigation Controller Garden")
    _add(hub, "switch.irrigation_controller_rain_delay", "off", "kitchen", friendly_name="Irrigation Controller Rain delay")
    _add(hub, "switch.irrigation_controller_garden_schedule", "off", "kitchen",
         friendly_name="Irrigation Controller Garden Schedule")
    assert kind_for(hub.home.states["switch.irrigation_controller_garden"]) == "valve"
    assert kind_for(hub.home.states["switch.irrigation_controller_rain_delay"]) == "switch"
    assert kind_for(hub.home.states["switch.irrigation_controller_garden_schedule"]) == "switch"
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "valve.run", "target": "switch.irrigation_controller_garden", "params": {}})
    assert r.status_code == 200, r.text
    assert ("switch", "turn_on", {"entity_id": "switch.irrigation_controller_garden"}) in hub.calls
    left = hub.actions.valve_remaining("switch.irrigation_controller_garden")
    assert left is not None and 590 <= left <= 600  # a start with no time still stops itself after 10 min
    hub.home.states["switch.irrigation_controller_garden"]["state"] = "on"
    zone = next(d for d in home_client.get("/v1/home", headers=dylan.h).json()["devices"]
                if d["id"] == "switch.irrigation_controller_garden")
    assert zone["kind"] == "valve" and zone["state"]["remaining_s"] > 500
    dylan.secure_post("/v1/actions", {"action": "valve.run", "target": "switch.irrigation_controller_garden",
                                    "params": {"minutes": 25}})
    assert len(hub.actions._valve_timers("switch.irrigation_controller_garden")) == 1  # replaced, not stacked
    dylan.secure_post("/v1/actions", {"action": "valve.run", "target": "switch.irrigation_controller_garden",
                                    "params": {"on": False}})
    assert ("switch", "turn_off", {"entity_id": "switch.irrigation_controller_garden"}) in hub.calls
    assert hub.actions.valve_remaining("switch.irrigation_controller_garden") is None


def test_sensors_unroomed_devices_and_weather(hub, home_client, people, ai):
    _add(hub, "binary_sensor.theo_closet", "on", "theo_room", friendly_name="Theos Closet Door", device_class="door")
    _add(hub, "binary_sensor.kitchen_motion", "off", "kitchen", friendly_name="Kitchen Motion", device_class="motion")
    _add(hub, "binary_sensor.cat_outside", "off", None, friendly_name="cat_is_outside", device_class="presence")
    _add(hub, "binary_sensor.dead", "unavailable", "kitchen", friendly_name="Motion", device_class="motion")
    _add(hub, "light.porch", "on", None, friendly_name="Porch")

    class Wx:
        data = {"now": {"temp": 61, "words": "Cloudy"}}

        def now(self):
            return self.data["now"]
    hub.smart.weather = Wx()
    fake = ai([[("house", {})], "ok"])
    ask(home_client, people["phones"]["Avery"], "anything open?")
    res = fake.results(1)[0]
    sensors = {x["id"]: x for x in res["sensors"]}
    assert sensors["binary_sensor.theo_closet"]["state"] == "open"
    assert sensors["binary_sensor.kitchen_motion"]["state"] == "clear"
    assert sensors["binary_sensor.cat_outside"]["state"] == "away" and "binary_sensor.dead" not in sensors
    assert any(d["id"] == "light.porch" and d["room"] is None for d in res["devices"])
    assert res["weather"]["now"]["temp"] == 61
    # A child sees sensors only in their own rooms, and nothing without a room
    fake = ai([[("house", {"kind": "sensor"})], "ok"])
    ask(home_client, people["phones"]["Theo"], "is my closet open")
    kid = fake.results(1)[0]
    assert [x["id"] for x in kid["sensors"]] == ["binary_sensor.theo_closet"]
    assert all(d["id"] != "light.porch" for d in kid["devices"])


def test_look_sends_a_picture_once(hub, home_client, people, ai):
    async def jpg(*_a, **_k):
        return b"\xff\xd8\xff fake jpeg"
    hub.frigate.latest_jpg = jpg
    hub.ha.camera_proxy = jpg
    fake = ai([[("look", {"camera": "driveway"})], "A white car is in the driveway."])
    r = ask(home_client, people["phones"]["Avery"], "is there a car in the driveway?")
    assert r.json()["reply"].startswith("A white car")
    block = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert block[0]["type"] == "image" and block[0]["source"]["media_type"] == "image/jpeg"
    # The next turn doesn't re-send the picture
    fake = ai(["Still there."])
    ask(home_client, people["phones"]["Avery"], "and now?", conversation_id=r.json()["conversation_id"])
    assert '"type": "image"' not in json.dumps(fake.requests[0]["messages"])
    # Children without the cameras permission get no picture
    fake = ai([[("look", {"camera": "driveway"})], "I can't."])
    ask(home_client, people["phones"]["Theo"], "show me the driveway")
    assert "not allowed" in fake.results(1)[0]["error"]


def test_nas_app_restart_needs_admin_face_id_and_spares_the_hub(hub, home_client, people):
    hub.config.hub.dry_run = False
    restarted = []

    async def restart(name):
        restarted.append(name)
    hub.truenas.restart_app = restart
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "system.app_restart", "target": "plex"})
    assert r.status_code == 200, r.text
    assert restarted == ["plex"]
    assert dylan.secure_post("/v1/actions", {"action": "system.app_restart", "target": "thomascreek-hub"}).status_code == 400
    avery = people["phones"]["Avery"]
    assert avery.secure_post("/v1/actions", {"action": "system.app_restart", "target": "plex"}).status_code == 403


def test_chores_and_trips_wait_for_a_tap(hub, home_client, people, ai):
    fake = ai([[("hub_change", {"method": "POST", "path": "/v1/family/items", "summary": "Add chore: test chore",
                                "body": {"title": "test chore", "kind": "chore", "people": ["Theo"]}})],
               "Tap Confirm to add it."])
    r = ask(home_client, people["phones"]["Avery"], "add a chore: test chore for Theo").json()
    assert [c["kind"] for c in r["confirms"]] == ["tap"] and r["confirms"][0]["path"] == "/v1/family/items"
    assert r["changed"] is False
    fake = ai([[("hub_change", {"method": "POST", "path": "/v1/trip-tools/create_trip", "body": {}})], "Confirm?"])
    assert ask(home_client, people["phones"]["Avery"], "plan a beach trip").json()["confirms"][0]["kind"] == "tap"


def test_look_never_hands_over_an_offline_or_unknown_camera(hub, home_client, people, ai):
    async def jpg(*_a, **_k):
        return b"\xff\xd8 stale frame"
    hub.frigate.latest_jpg = jpg
    hub.config.frigate.enabled = True
    hub.config.frigate.url = "http://frigate.test:5000"
    hub.frigate._info = {"140_driveway": {"online": False}, "140_creek": {"online": True}, "140_creek_ptz": {"online": True}}
    fake = ai([[("look", {"camera": "driveway"})], "I couldn't get a picture."])
    ask(home_client, people["phones"]["Avery"], "show me the driveway camera")
    res = fake.results(1)[0]
    assert "offline" in res["error"] and "don't describe" in res["error"]
    assert '"type": "image"' not in json.dumps(fake.requests[1]["messages"])
    fake = ai([[("look", {"camera": "creek"})], "Which one?"])
    ask(home_client, people["phones"]["Avery"], "show me the creek")
    assert "More than one" in fake.results(1)[0]["error"] and len(fake.results(1)[0]["cameras"]) == 2
    fake = ai([[("look", {"camera": "creek ptz"})], "Clear."])
    ask(home_client, people["phones"]["Avery"], "show me the creek ptz")
    assert fake.requests[1]["messages"][-1]["content"][0]["content"][0]["type"] == "image"
