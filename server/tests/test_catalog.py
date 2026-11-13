"""TV picking, appliance cards, and the admin's house layout (hide for everyone, room editor, scenes)."""

from __future__ import annotations

from hub.catalog import appliance_type, norm_name, pick_screens, screen_score

OLD = "2026-10-01T00:00:00+00:00"
NOW = "2026-11-13T10:00:00+00:00"


def mp(eid, name, state="idle", dc=None, changed=NOW, **attrs):
    a = {"friendly_name": name, **attrs}
    if dc:
        a["device_class"] = dc
    return {"entity_id": eid, "state": state, "attributes": a, "last_changed": changed, "last_updated": changed}


# Shaped like the house's real Home Assistant (Nov 13): every TV three or four times, plus junk.
TVS = [
    mp("media_player.adelines_tv", "Adeline’s  TV", dc="tv"),                       # cast
    mp("media_player.adelines_tv_2", "Adeline’s  TV", "off"),                         # androidtv_remote
    mp("media_player.adelines_tv_plus", "Adeline’s  TV+", "unavailable", dc="speaker"),
    mp("media_player.adelines_tv_dlna", "Adeline’s TV SmartTV 4K Television", "off", dc="tv"),  # dlna, has the room
    mp("media_player.basement_wall_tablet", "Basement Wall Tablet"),
    mp("media_player.office_voice", "Office Voice Assistant Media Player", "unavailable", dc="speaker"),
    mp("media_player.googletv3884", "GoogleTV3884", "unavailable", changed=OLD),
    mp("media_player.plex_shield", "Plex (Plex for Android (TV) - SHIELD Android TV)", "unavailable"),
    mp("media_player.macbook", "Dylan’s MacBook Pro (2)", dc="speaker"),
    mp("media_player.living_room", "Living Room", "playing", dc="speaker", group_members=["media_player.living_room"]),
    mp("media_player.basement_apple_tv", "Basement Apple TV", "off"),
    mp("media_player.denon", "Denon AVR-S730H", "off", dc="receiver"),
    mp("media_player.roku_premiere", "Roku Premiere", dc="speaker"),
    mp("media_player.kids_shield", "Kids Shield", "unavailable", dc="tv", changed=OLD),
]
PLATFORMS = {"media_player.adelines_tv": "cast", "media_player.adelines_tv_2": "androidtv_remote",
             "media_player.adelines_tv_dlna": "dlna_dmr", "media_player.basement_wall_tablet": "fully_kiosk",
             "media_player.office_voice": "esphome", "media_player.basement_apple_tv": "apple_tv",
             "media_player.plex_shield": "plex", "media_player.living_room": "sonos", "media_player.roku_premiere": "roku"}


def _pick(keep=()):
    states = {s["entity_id"]: s for s in TVS}
    area = {"media_player.adelines_tv_dlna": "adeline", "media_player.basement_wall_tablet": "basement",
            "media_player.basement_apple_tv": "basement"}
    return pick_screens(states, PLATFORMS, {}, {}, area, set(keep),
                        now=1794560000.0)  # 2026-11-13


def test_one_tv_per_real_tv_and_no_junk():
    screens, borrowed = _pick()
    assert screens == {"media_player.adelines_tv_2", "media_player.basement_apple_tv", "media_player.roku_premiere"}
    # The remote-capable copy is kept and takes the room from the DLNA copy.
    assert borrowed == {"media_player.adelines_tv_2": "adeline"}


def test_kept_player_stands_for_its_tv():
    screens, _ = _pick(keep={"media_player.adelines_tv"})
    assert "media_player.adelines_tv" in screens and "media_player.adelines_tv_2" not in screens


def test_names_and_scores():
    assert norm_name("Adeline’s TV SmartTV 4K Television") == norm_name("Adeline’s  TV") == norm_name("Adeline’s  TV+")
    assert screen_score(TVS[4], "fully_kiosk", None) is None
    assert screen_score(TVS[4], None, None) is None  # "Wall Tablet" by name alone
    assert screen_score(mp("media_player.x", "Master Bedroom TV", dc="tv"), "cast", None) == 2


def test_appliance_types():
    states = {
        "water_heater.vv142830_fridge": mp("water_heater.vv142830_fridge", "VV142830 Fridge", "unavailable"),
        "water_heater.vv142830_freezer": mp("water_heater.vv142830_freezer", "VV142830 Freezer", "unavailable"),
        "binary_sensor.tz_door": mp("binary_sensor.tz_door", "TZ747752B Dishwasher Door Status", "off", dc="door"),
        "sensor.tz_cycle": mp("sensor.tz_cycle", "TZ747752B Dishwasher Cycle State", "washing"),
        "sensor.dish_leak": mp("sensor.dish_leak", "Dishwasher leak sensor", "dry"),
    }
    fridge = {"id": "f1", "name": "VV142830", "integration": "ge_home",
              "entities": ["water_heater.vv142830_fridge", "water_heater.vv142830_freezer"]}
    dish = {"id": "d1", "name": "TZ747752B", "integration": "ge_home", "entities": ["binary_sensor.tz_door", "sensor.tz_cycle"]}
    leak = {"id": "l1", "name": "Dishwasher leak sensor", "integration": "zha", "model": "Water leak sensor",
            "entities": ["sensor.dish_leak"]}
    assert appliance_type(fridge, states) == "fridge"
    assert appliance_type(dish, states) == "dishwasher"
    assert appliance_type(leak, states) is None


def _with_appliances(hub):
    hub.home.states.update({
        "water_heater.vv142830_fridge": mp("water_heater.vv142830_fridge", "VV142830 Fridge", current_temperature=37),
        "water_heater.vv142830_freezer": mp("water_heater.vv142830_freezer", "VV142830 Freezer", current_temperature=0),
        "sensor.tz_cycle": mp("sensor.tz_cycle", "TZ747752B Dishwasher Cycle State", "washing"),
        "binary_sensor.tz_door": mp("binary_sensor.tz_door", "TZ747752B Dishwasher Door Status", "off", dc="door"),
        "sensor.kitchen_temp": mp("sensor.kitchen_temp", "Kitchen temperature", "71", dc="temperature",
                                  unit_of_measurement="°F"),
    })
    hub.home.entity_area["sensor.kitchen_temp"] = "kitchen"
    hub.home.ha_devices = {
        "abc123": {"id": "abc123", "name": "VV142830", "integration": "ge_home",
                   "entities": ["water_heater.vv142830_fridge", "water_heater.vv142830_freezer"]},
        "def456": {"id": "def456", "name": "TZ747752B", "integration": "ge_home",
                   "entities": ["sensor.tz_cycle", "binary_sensor.tz_door"]},
        "aaa999": {"id": "aaa999", "name": "Kitchen sensor", "integration": "zha", "model": "Temperature sensor",
                   "entities": ["sensor.kitchen_temp"]},
    }


def test_appliances_in_snapshot(hub, people):
    _with_appliances(hub)
    snap = people["phones"]["Dylan"].client.get("/v1/home", headers=people["phones"]["Dylan"].h).json()
    cards = {c["type"]: c for c in snap["appliances"]}
    assert set(cards) == {"fridge", "dishwasher"}  # the temperature sensor is not an appliance
    assert cards["fridge"]["name"] == "Fridge" and "37°" in cards["fridge"]["status"]
    assert cards["dishwasher"]["status"] == "Washing" and cards["dishwasher"]["running"] is True
    # The fridge is not a "shower" any more.
    assert not any(d["id"] == "water_heater.vv142830_fridge" for d in snap["devices"])
    theo = people["phones"]["Theo"]
    assert "appliances" not in theo.client.get("/v1/home", headers=theo.h).json()


def test_hide_for_everyone(hub, people):
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    r = dylan.client.post("/v1/home/hidden", json={"ids": ["light.kitchen"], "hidden": True}, headers=dylan.h)
    assert r.status_code == 200, r.text
    for ph in (dylan, avery):
        snap = ph.client.get("/v1/home", headers=ph.h).json()
        assert not any(d["id"] == "light.kitchen" for d in snap["devices"])
    lay = dylan.client.get("/v1/home/layout", headers=dylan.h).json()
    assert [x["id"] for x in lay["hidden"]] == ["light.kitchen"]
    # Hidden devices still work (modes, Ask, automations).
    r = dylan.client.post("/v1/actions", json={"action": "light.set", "target": "light.kitchen", "params": {"on": False}},
                          headers=dylan.h)
    assert r.status_code == 200, r.text
    dylan.client.post("/v1/home/hidden", json={"ids": ["light.kitchen"], "hidden": False}, headers=dylan.h)
    snap = avery.client.get("/v1/home", headers=avery.h).json()
    assert any(d["id"] == "light.kitchen" for d in snap["devices"])


def test_only_admin_edits_layout(people):
    avery = people["phones"]["Avery"]
    r = avery.client.post("/v1/home/hidden", json={"ids": ["light.kitchen"]}, headers=avery.h)
    assert r.status_code == 403
    assert avery.client.put("/v1/rooms/kitchen/layout", json={"name": "x"}, headers=avery.h).status_code == 403


def test_room_editor(hub, people):
    dylan = people["phones"]["Dylan"]
    r = dylan.client.put("/v1/rooms/living_room/layout", headers=dylan.h, json={
        "name": "Family room", "symbol": "tv", "add": ["light.kitchen"],
        "order": ["light.kitchen", "light.living_room"]})
    assert r.status_code == 200, r.text
    room = r.json()
    assert room["name"] == "Family room" and room["ha_name"] == "Living Room"
    assert [d["id"] for d in room["devices"]][:2] == ["light.kitchen", "light.living_room"]
    assert room["scenes"][0]["id"] == "scene.movie_night"
    snap = dylan.client.get("/v1/home", headers=dylan.h).json()
    lr = next(x for x in snap["rooms"] if x["id"] == "living_room")
    assert lr["name"] == "Family room" and lr["symbol"] == "tv"
    assert lr["device_ids"][:2] == ["light.kitchen", "light.living_room"]
    assert lr["scenes"] == [{"id": "scene.movie_night", "name": "Movie night", "icon": None}]
    # Moving the kitchen light out of the kitchen leaves the kitchen room empty, so it drops off.
    assert not any(x["id"] == "kitchen" for x in snap["rooms"])
    # Undo the move and hide the room.
    dylan.client.put("/v1/rooms/living_room/layout", headers=dylan.h, json={"remove": ["light.kitchen"], "hidden": True})
    snap = dylan.client.get("/v1/home", headers=dylan.h).json()
    assert not any(x["id"] == "living_room" for x in snap["rooms"])
    assert any(x["id"] == "kitchen" for x in snap["rooms"])


def test_scene_hidden_and_kid_scenes(hub, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    snap = theo.client.get("/v1/home", headers=theo.h).json()
    lr = next((x for x in snap["rooms"] if x["id"] == "living_room"), None)
    assert lr is not None  # Theo shares the living room
    dylan.client.post("/v1/home/hidden", json={"ids": ["scene.movie_night"]}, headers=dylan.h)
    snap = dylan.client.get("/v1/home", headers=dylan.h).json()
    lr = next(x for x in snap["rooms"] if x["id"] == "living_room")
    assert lr["scenes"] == []
    room = dylan.client.get("/v1/rooms/living_room/layout", headers=dylan.h).json()
    assert room["scenes"][0]["hidden"] is True


def test_cleanup_lists_long_dead(hub, people):
    dylan = people["phones"]["Dylan"]
    hub.home.states["light.old_strip"] = mp("light.old_strip", "Old strip", "unavailable", changed=OLD)
    hub.home.entity_area["light.old_strip"] = "kitchen"
    lay = dylan.client.get("/v1/home/layout", headers=dylan.h).json()
    assert [x["id"] for x in lay["cleanup"]] == ["light.old_strip"]
    r = dylan.client.post("/v1/home/hidden", json={"ids": [x["id"] for x in lay["cleanup"]]}, headers=dylan.h)
    assert r.status_code == 200
    assert dylan.client.get("/v1/home/layout", headers=dylan.h).json()["cleanup"] == []


def test_screens_filter_in_snapshot(hub, people):
    dylan = people["phones"]["Dylan"]
    for s in TVS:
        hub.home.states[s["entity_id"]] = s
    hub.home.platforms = PLATFORMS
    hub.home.entity_area.update({"media_player.basement_wall_tablet": "living_room",
                                 "media_player.adelines_tv_dlna": "living_room"})
    hub.home.ha_area = None
    hub.home.recompute()
    snap = dylan.client.get("/v1/home", headers=dylan.h).json()
    media = {d["id"] for d in snap["devices"] if d["kind"] == "media"}
    assert "media_player.basement_wall_tablet" not in media
    assert "media_player.adelines_tv_2" in media  # borrowed the living room from its DLNA copy
    assert "media_player.theo_tv" in media  # Theo's own TV always stays
