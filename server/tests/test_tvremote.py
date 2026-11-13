"""The full TV remote: keys per integration, app shortcuts, inputs, typing, and the kids' limits."""

from __future__ import annotations

from hub.home import device_state
from hub.tvremote import app_label, remote_info, text_keys

ANDROID = {"remote": "remote.theo_tv", "platform": "androidtv_remote"}


def _tv(hub, **attrs):
    async def instant(_s):
        return None
    hub.actions.pause = instant
    st = hub.home.states["media_player.theo_tv"]
    st["state"] = "on"
    st["attributes"].update(supported_features=128 | 256 | 1 | 16384 | 1024 | 4 | 8, **attrs)
    hub.config.hub.dry_run = False
    hub.home.media_links = {"media_player.theo_tv": dict(ANDROID)}
    return st


def _press(client, person, **params):
    return client.post("/v1/actions", json={"action": "media.command", "target": "media_player.theo_tv",
                                             "params": params}, headers=person.h)


def test_app_names_are_readable():
    assert app_label("com.google.android.apps.tv.launcherx") == "Home"
    assert app_label("com.netflix.ninja") == "Netflix"
    assert app_label("com.example.coolapp") == "Example"
    assert app_label("Netflix") == "Netflix"
    assert app_label(None) is None


def test_android_tv_gets_the_whole_remote(hub):
    st = _tv(hub, app_id="com.google.android.apps.tv.launcherx", app_name="com.google.android.apps.tv.launcherx",
             source=None, source_list=None)
    s = device_state("media", st, ANDROID)
    assert s["source"] == "Home"
    rc = s["remote"]
    assert {"up", "select", "back", "home", "menu", "ch_up", "ch_down", "input", "play_pause", "guide"} <= set(rc["keys"])
    assert rc["text"] is True and rc["inputs"] == []
    assert {"id": "youtube", "name": "YouTube", "kid": False} in rc["apps"]
    assert {"app", "text", "input", "ch_up"} <= set(s["commands"])


def test_other_tvs_split_apps_from_inputs():
    st = {"state": "on", "attributes": {"supported_features": 2048,
                                        "source_list": ["HDMI 1", "Live TV", "Netflix", "YouTube", "Plex"]}}
    rc = remote_info(st, {"remote": "remote.lg", "platform": "webostv"})
    assert rc["inputs"] == ["HDMI 1", "Live TV"]
    assert [a["id"] for a in rc["apps"]] == ["netflix", "youtube", "plex"]
    assert "ch_up" in rc["keys"] and rc["text"] is False
    apple = remote_info({"state": "on", "attributes": {"source_list": ["Netflix"]}},
                        {"remote": "remote.living", "platform": "apple_tv"})
    assert apple["keys"][:7] == ["up", "down", "left", "right", "select", "back", "home"] and "ch_up" not in apple["keys"]


def test_buttons_apps_and_typing_reach_home_assistant(hub, people, home_client):
    _tv(hub)
    dylan = people["phones"]["Dylan"]
    for params in ({"command": "menu"}, {"command": "ch_up"}, {"command": "input"},
                   {"command": "app", "app": "youtube"}, {"command": "text", "text": "Bluey s2!"}):
        r = _press(home_client, dylan, **params)
        assert r.status_code == 200, (params, r.text)
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "MENU"}) in hub.calls
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "CHANNEL_UP"}) in hub.calls
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "TV_INPUT"}) in hub.calls
    assert ("media_player", "play_media", {"entity_id": "media_player.theo_tv", "media_content_type": "app",
                                           "media_content_id": "com.google.android.youtube.tv"}) in hub.calls
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": text_keys("Bluey s2!"),
                                       "delay_secs": 0.05}) in hub.calls
    assert text_keys("Bluey s2!") == ["B", "L", "U", "E", "Y", "SPACE", "S", "2"]
    assert _press(home_client, dylan, command="app", app="rm -rf /").status_code == 400
    assert _press(home_client, dylan, command="exit").status_code == 400  # LG's key, not this TV's


def test_a_child_opens_only_kids_apps(hub, people, home_client):
    _tv(hub)
    theo = people["phones"]["Theo"]
    assert _press(home_client, theo, command="select").status_code == 200
    assert _press(home_client, theo, command="app", app="youtube_kids").status_code == 200
    r = _press(home_client, theo, command="app", app="youtube")
    assert r.status_code == 403, r.text



def test_the_same_app_button_works_on_any_tv(hub, people, home_client):
    st = _tv(hub, source_list=["HDMI 2", "YouTube", "Disney Plus"])
    hub.home.media_links = {"media_player.theo_tv": {"remote": "remote.lg", "platform": "webostv"}}
    dylan = people["phones"]["Dylan"]
    assert [a["id"] for a in device_state("media", st, hub.home.media_links["media_player.theo_tv"])["remote"]["apps"]] \
        == ["youtube", "disney_plus"]
    for app in ("youtube", "Disney+"):
        assert _press(home_client, dylan, command="app", app=app).status_code == 200
    assert ("media_player", "select_source", {"entity_id": "media_player.theo_tv", "source": "YouTube"}) in hub.calls
    assert ("media_player", "select_source", {"entity_id": "media_player.theo_tv", "source": "Disney Plus"}) in hub.calls
    assert _press(home_client, dylan, command="app", app="netflix").status_code == 400  # not on this TV
    assert _press(home_client, dylan, command="source", source="HDMI 2").status_code == 200


def test_watch_anywhere_lists_apps_with_the_tvs_that_have_them(hub, people, home_client):
    _tv(hub)
    r = home_client.get("/v1/media/watch", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    apps = {a["id"]: a for a in r.json()["apps"]}
    assert [s["entity_id"] for s in apps["youtube"]["screens"]] == ["media_player.theo_tv"]
    kid = home_client.get("/v1/media/watch", headers=people["phones"]["Theo"].h).json()["apps"]
    assert kid and all(a["kid"] for a in kid) and "youtube" not in {a["id"] for a in kid}


def test_opening_an_app_turns_the_tv_on_first(hub, people, home_client):
    st = _tv(hub)
    st["state"] = "off"
    assert _press(home_client, people["phones"]["Dylan"], command="app", app="plex").status_code == 200
    names = [c[:2] for c in hub.calls]
    i = names.index(("media_player", "turn_on"))
    # still off a moment later, so the remote's wake-up goes too, then the app opens
    assert names[i + 1:i + 3] == [("remote", "turn_on"), ("media_player", "play_media")]


def test_a_tv_that_lost_its_connection_is_reconnected_and_the_press_retried(hub, people, home_client):
    _tv(hub)
    hub.home.states["remote.theo_tv"] = {"entity_id": "remote.theo_tv", "state": "unavailable", "attributes": {}}
    sent = []

    async def flaky(domain, service, data):
        sent.append((domain, service))
        if (domain, service) == ("remote", "send_command") and sent.count(("remote", "send_command")) == 1:
            raise RuntimeError("connection reset")
    hub.ha.call_service = flaky
    assert _press(home_client, people["phones"]["Dylan"], command="up").status_code == 200
    assert sent == [("homeassistant", "reload_config_entry"), ("remote", "send_command"),
                    ("homeassistant", "reload_config_entry"), ("remote", "send_command")]

    async def dead(domain, service, data):
        raise RuntimeError("timeout")
    hub.ha.call_service = dead
    r = _press(home_client, people["phones"]["Dylan"], command="up")
    assert r.status_code == 502 and "plugged in" in r.json()["message"]


def _box(hub, tv_state="off", tv_source="HDMI 1"):
    """Theo's TV is an Android box plugged into the Samsung TV on HDMI 2."""
    _tv(hub)
    hub.home.states["media_player.samsung"] = {
        "entity_id": "media_player.samsung", "state": tv_state,
        "attributes": {"friendly_name": "Samsung TV", "supported_features": 2048 | 4 | 8 | 128 | 256,
                       "source": tv_source, "source_list": ["TV", "HDMI 1", "HDMI 2", "Netflix"]}}


def test_saying_which_tv_a_box_is_plugged_into(hub, people, home_client):
    _box(hub)
    dylan, theo = people["phones"]["Dylan"].h, people["phones"]["Theo"].h
    assert home_client.put("/v1/media/hookups/media_player.theo_tv", headers=theo,
                           json={"display_entity_id": "media_player.samsung", "input": "HDMI 2"}).status_code == 403
    assert home_client.put("/v1/media/hookups/media_player.theo_tv", headers=dylan,
                           json={"display_entity_id": "media_player.samsung", "input": "HDMI 9"}).status_code == 400
    r = home_client.put("/v1/media/hookups/media_player.theo_tv", headers=dylan,
                        json={"display_entity_id": "media_player.samsung", "input": "HDMI 2"})
    assert r.status_code == 200, r.text
    assert r.json()["plugged_into"] == {"entity_id": "media_player.samsung", "name": "Samsung TV", "input": "HDMI 2"}
    screens = {s["entity_id"]: s for s in home_client.get("/v1/media/hookups", headers=dylan).json()["screens"]}
    assert screens["media_player.samsung"]["inputs"] == ["TV", "HDMI 1", "HDMI 2"]
    assert screens["media_player.samsung"]["boxes"] == ["media_player.theo_tv"]
    # a box can't be "plugged into" another box
    assert home_client.put("/v1/media/hookups/media_player.samsung", headers=dylan,
                           json={"display_entity_id": "media_player.theo_tv"}).status_code == 400
    assert home_client.put("/v1/media/hookups/media_player.theo_tv", headers=dylan,
                           json={"display_entity_id": None}).json()["plugged_into"] is None


def test_using_the_box_wakes_its_tv_and_switches_the_input(hub, people, home_client):
    _box(hub)
    hub.hookups.set("media_player.theo_tv", "media_player.samsung", "HDMI 2")
    dylan = people["phones"]["Dylan"]
    assert _press(home_client, dylan, command="app", app="youtube").status_code == 200
    names = [(c[0], c[1], c[2].get("entity_id")) for c in hub.calls]
    assert names[:3] == [("media_player", "turn_on", "media_player.samsung"),
                         ("media_player", "select_source", "media_player.samsung"),
                         ("media_player", "play_media", "media_player.theo_tv")]
    assert hub.calls[1][2]["source"] == "HDMI 2"
    # TV already on the right input: just the box
    hub.calls.clear()
    hub.home.states["media_player.samsung"].update(state="on")
    hub.home.states["media_player.samsung"]["attributes"]["source"] = "HDMI 2"
    assert _press(home_client, dylan, command="up").status_code == 200
    assert [c[2]["entity_id"] for c in hub.calls] == ["remote.theo_tv"]
    # volume goes to the TV, power turns both off
    hub.calls.clear()
    assert _press(home_client, dylan, command="vol_up").status_code == 200
    assert _press(home_client, dylan, command="power").status_code == 200
    assert hub.calls == [("media_player", "volume_up", {"entity_id": "media_player.samsung"}),
                         ("media_player", "turn_off", {"entity_id": "media_player.theo_tv"}),
                         ("media_player", "turn_off", {"entity_id": "media_player.samsung"})]
    devs = home_client.get("/v1/home", headers=dylan.h).json()["devices"]
    box = next(d for d in devs if d["id"] == "media_player.theo_tv")
    assert box["state"]["plugged_into"]["name"] == "Samsung TV"
