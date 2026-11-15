"""The phone sees every TV and changes one: /v1/tvs, settings, actions and config_changed."""

from __future__ import annotations

import time

from hub.routes.home import shape_event
from hub.tvapp import AdbResult
from hub.tvfleet import report

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _config, _enroll, _token
from tests.test_tv_fleet import BODY


def _tv(hub, home_client, key: Key | None = None):
    key = key or Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    token = _token(home_client, key, Clock())["access_token"]
    return key, _auth(token)


def _screen(hub):
    subject = hub.tv_app.subject_for(hub.tv_app.tvs()[TV_ID], TV_ID, None)
    return hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), None, device_id=f"tv:{TV_ID}", tv=True)


def _drain(queue) -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_the_phone_lists_every_tv_with_what_is_on(hub, home_client, people):
    _, headers = _tv(hub, home_client)
    dylan, avery, theo = (people["phones"][n] for n in ("Dylan", "Avery", "Theo"))
    report(hub, TV_ID, BODY, time.time())
    posted = home_client.post("/v1/tv/state", headers=headers, json={
        "foreground": True,
        "playing": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom", "name": "CNN", "kind": "live"},
        "health": {"app_version": "1.5.0", "last_error": "EPG timed out"},
    })
    assert posted.status_code == 200, posted.text

    assert theo.client.get("/v1/tvs", headers=theo.h).status_code == 403
    page = avery.client.get("/v1/tvs", headers=avery.h)
    assert page.status_code == 200, page.text
    row = page.json()["tvs"][0]
    assert row["id"] == TV_ID and row["name"] == "Living room" and row["room"] == "living_room"
    assert row["online"] is True and row["health"] == "ok" and row["status"] == "approved"
    assert row["app_version"] == "1.5.0" and row["last_error"] == "EPG timed out"
    assert row["now_playing"] == {"kind": "live", "title": "Newsroom", "name": "CNN", "channel_key": "xc:thomas-creek:10"}
    assert row["model"] == "onn. 4K Streaming Box" and row["screenshot"] is None and row["settings_rev"] == 0

    one = dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()
    assert one["device"]["cpu_pct"] == 12 and one["settings"] == {} and "restart_app" in one["actions"]
    assert dylan.client.get("/v1/tvs/tv_nope_0000000000", headers=dylan.h).status_code == 404


def test_a_quiet_tv_is_offline_and_shows_nothing_playing(hub, home_client, people):
    _tv(hub, home_client)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(playing={"title": "Old"}, state_at=time.time() - 600, last_seen_at=time.time() - 600,
                      last_config_at=time.time() - 600)
    hub.tv_app._save_tvs(tvs)
    dylan = people["phones"]["Dylan"]
    row = dylan.client.get("/v1/tvs", headers=dylan.h).json()["tvs"][0]
    assert row["online"] is False and row["health"] == "offline" and row["now_playing"] is None


def test_a_phone_change_reaches_the_tv_config_at_once(hub, home_client, people):
    key, headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    clock = Clock()
    first = _config(home_client, key, clock.next())
    assert first.json()["settings"] == {} and first.json()["settings_rev"] == 0
    tag = first.headers["etag"]
    screen = _screen(hub)

    changed = dylan.client.patch(f"/v1/tvs/{TV_ID}/settings", headers=dylan.h, json={"settings": {
        "favorites": ["xc:thomas-creek:10", "xc:thomas-creek:10", "iptv-org:CNN.us"],
        "hidden_categories": ["Adult"], "parental": {"locked": True, "max_rating": "PG"},
        "home_rows": ["continue", "live_now"], "language": "en-US",
        "player": {"autoplay_next": False, "subtitles": "auto"},
    }})
    assert changed.status_code == 200, changed.text
    assert changed.json()["settings_rev"] == 1
    assert changed.json()["settings"]["favorites"] == ["xc:thomas-creek:10", "iptv-org:CNN.us"]
    events = _drain(screen.queue)
    assert [e["type"] for e in events] == ["config_changed"]
    assert events[0]["data"]["install_id"] == TV_ID and events[0]["data"]["settings_rev"] == 1

    pulled = _config(home_client, key, clock.next(), {"If-None-Match": tag})
    assert pulled.status_code == 200
    assert pulled.json()["settings"]["parental"] == {"locked": True, "max_rating": "PG"}
    assert pulled.json()["settings_rev"] == 1

    # null removes a setting; rename and room in the same call send one event.
    again = dylan.client.patch(f"/v1/tvs/{TV_ID}/settings", headers=dylan.h, json={
        "name": "Family room", "room": "living_room", "settings": {"language": None}})
    assert again.status_code == 200, again.text
    assert "language" not in again.json()["settings"] and again.json()["settings_rev"] == 2
    assert [e["type"] for e in _drain(screen.queue)] == ["config_changed"]
    assert hub.tv_app.tvs()[TV_ID]["name"] == "Family room"

    for bad, message in (
        ({"settings": {"picture": {"mode": "vivid"}}}, "Picture and audio stay on each TV."),
        ({"settings": {"volume": 3}}, "volume isn't a TV setting the hub keeps."),
        ({"settings": {"favorites": ["http://evil.example/x"]}}, "favorites has an entry the hub can't use."),
        ({"settings": {"parental": {"pin": "1234"}}}, "parental takes locked and max_rating. A PIN stays on the TV."),
        ({"profile": "teen"}, "Profile is adult or kids."),
    ):
        refused = dylan.client.patch(f"/v1/tvs/{TV_ID}/settings", headers=dylan.h, json=bad)
        assert refused.status_code == 400 and refused.json()["message"] == message, refused.text
    assert _drain(screen.queue) == []
    theo = people["phones"]["Theo"]
    assert theo.client.patch(f"/v1/tvs/{TV_ID}/settings", headers=theo.h, json={"settings": {}}).status_code == 403


def test_a_change_on_the_tv_is_kept_and_the_last_write_wins(hub, home_client, people):
    _, headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    dylan.client.patch(f"/v1/tvs/{TV_ID}/settings", headers=dylan.h, json={"settings": {"hidden_categories": ["Adult"]}})
    from_tv = home_client.post("/v1/tv/settings", headers=headers,
                               json={"settings": {"hidden_categories": ["Adult", "Sports"]}, "base_rev": 0})
    assert from_tv.status_code == 200, from_tv.text
    assert from_tv.json()["settings"]["hidden_categories"] == ["Adult", "Sports"] and from_tv.json()["settings_rev"] == 2
    # The TV can't rename or move itself through this route.
    home_client.post("/v1/tv/settings", headers=headers, json={"settings": {}, "name": "Hacked"})
    assert hub.tv_app.tvs()[TV_ID]["name"] == "Living room"
    assert dylan.client.post("/v1/tv/settings", headers=dylan.h, json={"settings": {}}).status_code == 403


def test_kid_and_lineup_changes_reach_every_tv(hub, home_client):
    _tv(hub, home_client)
    screen = _screen(hub)
    hub.tv_app.set_kids_categories(["Kids"])
    events = _drain(screen.queue)
    assert [e["type"] for e in events] == ["config_changed"] and events[0]["data"]["install_id"] is None


def test_actions_go_through_the_tv_app_when_it_is_online(hub, home_client, people):
    _, headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    report(hub, TV_ID, BODY, time.time())
    screen = _screen(hub)

    restart = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "restart_app"})
    assert restart.status_code == 200, restart.text
    assert restart.json()["via"] == "app"
    basic = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "screenshot"})
    assert basic.status_code == 409 and basic.json()["error"] in ("no_adb", "no_address")
    assert dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["can_screenshot"] is False
    report(hub, TV_ID, dict(BODY, capability="root"), time.time())
    shot = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "screenshot"})
    assert shot.json()["via"] == "app"
    events = _drain(screen.queue)
    assert [e["data"]["action"] for e in events] == ["restart_app", "screenshot"]

    png = b"\x89PNG\r\n\x1a\nfake"
    saved = home_client.post(f"/v1/tv/fleet/screenshot?command={shot.json()['id']}", headers=headers, content=png)
    assert saved.status_code == 200, saved.text
    row = dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()
    assert row["screenshot"]["url"].startswith(f"/v1/tvs/{TV_ID}/screenshot?t=")
    picture = dylan.client.get(row["screenshot"]["url"], headers=dylan.h)
    assert picture.status_code == 200 and picture.content == png

    play = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h,
                             json={"action": "play", "channel_key": "xc:thomas-creek:10"})
    assert play.status_code == 200, play.text
    sent = _drain(screen.queue)
    assert sent[0]["type"] == "tv_command" and sent[0]["data"]["channel_key"] == "xc:thomas-creek:10"
    bad = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "self_destruct"})
    assert bad.status_code == 400
    no_build = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "update"})
    assert no_build.status_code == 409 and no_build.json()["message"] == "The hub doesn't have a TV app build yet."


def test_an_offline_tv_is_restarted_over_adb(hub, home_client, people):
    _tv(hub, home_client)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(ip="10.0.1.21", state_at=0, last_seen_at=0, last_config_at=0)
    hub.tv_app._save_tvs(tvs)
    calls: list[list[str]] = []
    replies = {"connect": "connected to 10.0.1.21:5555", "get-state": "device"}

    async def fake(args, timeout):
        calls.append(args)
        if args[0] == "connect":
            return AdbResult(0, replies["connect"])
        if "get-state" in args:
            return AdbResult(0, replies["get-state"])
        return AdbResult(0, "Events injected: 1")

    hub.tv_app.adb._run = fake
    hub.tv_app.adb.binary = "sh"  # anything on PATH, so `available` is true
    dylan = people["phones"]["Dylan"]
    restart = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "restart_app"})
    assert restart.status_code == 200, restart.text
    assert restart.json()["via"] == "adb"
    assert any("force-stop" in call for call in calls) and any("monkey" in call for call in calls)

    png = b"\x89PNG\r\n\x1a\nscreen"

    async def raw(args, timeout):
        calls.append(args)
        return png

    hub.tv_app.adb.raw_runner = raw
    assert dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["can_screenshot"] is True
    taken = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "screenshot"})
    assert taken.status_code == 200, taken.text
    assert taken.json()["via"] == "adb" and ["-s", "10.0.1.21:5555", "exec-out", "screencap", "-p"] in calls
    url = dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["screenshot"]["url"]
    assert dylan.client.get(url, headers=dylan.h).content == png

    replies["get-state"] = "unauthorized"
    blocked = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "open_app"})
    assert blocked.status_code == 409
    assert blocked.json()["message"] == "Living room needs one Allow for the hub: press Allow on the TV, then try again."
    shot = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "screenshot"})
    assert shot.status_code == 409 and shot.json()["error"] == "needs_allow"


def test_phones_never_see_config_changed(hub, people):
    dylan = hub.subject_for(hub.db.device(people["phones"]["Dylan"].device_id), True)
    assert shape_event(hub, dylan, {"type": "config_changed", "data": {"install_id": None}}) is None

