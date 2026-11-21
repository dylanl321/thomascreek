"""One remote for every TV: the list a phone draws and the one press route (hub/tvpress.py)."""

from __future__ import annotations

import time

from hub import tvpress
from hub.routes.home import shape_event
from hub.tvapp import AdbResult
from hub.tvfleet import report

from tests.test_tv_api import KID_TV, TV_ID, Clock, Key, _auth, _enroll, _token
from tests.test_tv_fleet import BODY
from tests.test_tv_handoff import BED, _two_tvs
from tests.test_tvremote import _tv as _ha_tv

LIST = "/v1/tv/phone/tvs"


def _press(person, tv, **body):
    return person.client.post(f"{LIST}/{tv}/press", headers=person.h, json=body)


def _row(person, tv):
    page = person.client.get(LIST, headers=person.h)
    assert page.status_code == 200, page.text
    return next(row for row in page.json()["tvs"] if row["id"] == tv)


def _on_screen(home_client, key, install_id=TV_ID, **state):
    auth = _auth(_token(home_client, key, Clock(), install_id)["access_token"])
    posted = home_client.post("/v1/tv/state", headers=auth, json={"foreground": True, **state})
    assert posted.status_code == 200, posted.text


def _asleep(hub, install_id):
    tvs = hub.tv_app.tvs()
    old = time.time() - 600
    tvs[install_id].update(state_at=old, last_seen_at=old, last_config_at=old)
    hub.tv_app._save_tvs(tvs)


def _events(hub, install_id=TV_ID):
    subject = hub.tv_app.subject_for(hub.tv_app.tvs()[install_id], install_id, None)
    return hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), None, device_id=f"tv:{install_id}", tv=True)


def _adb(hub, install_id=TV_ID, fail=False):
    tvs = hub.tv_app.tvs()
    tvs[install_id].update(ip="10.0.1.21")
    hub.tv_app._save_tvs(tvs)
    calls: list[list[str]] = []

    async def fake(args, _wait):
        calls.append(args)
        if args[0] == "connect":
            return AdbResult(0, "connected to 10.0.1.21:5555")
        if "get-state" in args:
            return AdbResult(0, "device")
        if fail and "keyevent" in args:
            return AdbResult(1, "error: closed")
        return AdbResult(0, "")

    hub.tv_app.adb._run = fake
    hub.tv_app.adb.binary = "sh"
    tvpress._adb_mark(hub, install_id, True)  # the box answered a moment ago
    return calls


def test_a_tv_playing_the_app_offers_the_player_buttons_and_nothing_dead(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())  # an Android box
    _on_screen(home_client, office, playing={"channel_key": "xc:thomas-creek:10", "title": "Newsroom", "name": "CNN",
                                             "kind": "live"})
    dylan = people["phones"]["Dylan"]
    row = _row(dylan, TV_ID)
    assert row["name"] == "Living room" and row["online"] is True and row["kids"] is False
    assert row["now_playing"]["title"] == "Newsroom"
    # No Home Assistant remote and no network debugging: the app's own buttons, and no arrows.
    # Thomas Creek TV is in front, so there is nothing to open.
    assert row["controls"] == ["back", "home", "play", "pause", "seek", "stop", "channel_up",
                               "channel_down", "tune", "send", "volume_up", "volume_down", "mute", "text"]
    assert "note" not in row

    _asleep(hub, BED)
    bed = _row(dylan, BED)
    assert bed["online"] is False and bed["controls"] == [] and "off or asleep" in bed["note"]
    refused = _press(dylan, BED, key="pause")
    assert refused.status_code == 409 and "off or asleep" in refused.json()["message"]
    missing = _press(dylan, TV_ID, key="up")
    assert missing.status_code == 409
    assert missing.json()["message"] == "Living room doesn't have that button from a phone."
    assert _press(dylan, TV_ID, key="self_destruct").status_code == 400


def test_a_player_press_goes_down_the_tvs_own_stream(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    screen = _events(hub)
    dylan = people["phones"]["Dylan"]

    paused = _press(dylan, TV_ID, key="pause")
    assert paused.status_code == 200, paused.text
    assert paused.json() == {"ok": True, "via": "app", "message": "Paused on Living room."}
    assert screen.queue.get_nowait()["data"]["command"] == "pause"

    skipped = _press(dylan, TV_ID, key="seek", position_s=125.4)
    assert skipped.json()["via"] == "app" and screen.queue.get_nowait()["data"]["position_s"] == 125
    assert _press(dylan, TV_ID, key="seek").status_code == 400

    up = _press(dylan, TV_ID, key="channel_up")
    assert up.status_code == 200 and screen.queue.get_nowait()["data"]["command"] == "channel_up"
    tuned = _press(dylan, TV_ID, key="tune", channel_key="xc:thomas-creek:10")
    assert tuned.status_code == 200 and tuned.json()["message"] == "Tuned on Living room."
    stopped = _press(dylan, TV_ID, key="stop")
    assert stopped.status_code == 200 and stopped.json()["via"] == "app"
    assert hub.tv_app.pending_commands(TV_ID)[-1]["action"] == "stop"


def test_an_apple_tv_is_offered_only_what_its_app_acts_on(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    _on_screen(home_client, office)  # no Android report, no address: not an Android box
    dylan = people["phones"]["Dylan"]
    # Nothing is playing yet: no player buttons to press into the air.
    assert _row(dylan, TV_ID)["controls"] == ["tune", "send"]
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["playing"] = {"movie_id": "m1", "title": "A film", "kind": "movie"}
    hub.tv_app._save_tvs(tvs)
    assert _row(dylan, TV_ID)["controls"] == ["play", "pause", "seek", "stop", "tune", "send"]
    assert _press(dylan, TV_ID, key="channel_up").status_code == 409


def test_arrows_go_over_network_debugging_and_one_connect_serves_a_held_arrow(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    calls = _adb(hub)
    dylan = people["phones"]["Dylan"]
    row = _row(dylan, TV_ID)
    assert {"up", "down", "left", "right", "select", "menu", "play_pause", "power_on", "power_off",
            "input"} <= set(row["controls"])

    for key in ("down", "down", "down", "select"):
        sent = _press(dylan, TV_ID, key=key)
        assert sent.status_code == 200, sent.text
        assert sent.json()["via"] == "adb"
    assert [c[-1] for c in calls if "keyevent" in c] == ["KEYCODE_DPAD_DOWN"] * 3 + ["KEYCODE_DPAD_CENTER"]
    assert len([c for c in calls if c[0] == "connect"]) == 1

    typed = _press(dylan, TV_ID, key="text", text="Bluey s2; rm -rf /")
    assert typed.status_code == 200 and typed.json()["via"] == "app"  # the app is on screen: its own search


def test_typing_over_network_debugging_sends_nothing_a_shell_would_run(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["foreground"] = False  # another app is on screen
    hub.tv_app._save_tvs(tvs)
    calls = _adb(hub)
    dylan = people["phones"]["Dylan"]
    typed = _press(dylan, TV_ID, key="text", text="Bluey s2; rm -rf / $(reboot) `x` 'q'")
    assert typed.status_code == 200 and typed.json()["via"] == "adb", typed.text
    assert calls[-1][-3:] == ["input", "text", "Bluey%ss2%srm%s-rf%s%sreboot%sx%sq"]
    assert _press(dylan, TV_ID, key="text", text="http://x.example/a").status_code == 400
    away = _press(dylan, TV_ID, key="pause")
    assert away.status_code == 409 and "isn't showing Thomas Creek TV" in away.json()["message"]


def test_a_key_that_fails_says_so_and_the_next_press_reconnects(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    calls = _adb(hub, fail=True)
    dylan = people["phones"]["Dylan"]
    failed = _press(dylan, TV_ID, key="up")
    assert failed.status_code == 502
    assert failed.json()["message"] == "Living room didn't take that. Try again in a moment."
    # The box stopped taking keys: its arrows are gone from the next list until it answers again.
    assert "up" not in _row(dylan, TV_ID)["controls"]
    assert _press(dylan, TV_ID, key="up").status_code == 409
    tvpress._adb_mark(hub, TV_ID, True)
    _press(dylan, TV_ID, key="up")
    assert len([c for c in calls if c[0] == "connect"]) == 2


def test_volume_falls_back_to_the_tv_app_when_the_key_fails(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    _adb(hub, fail=True)
    dylan = people["phones"]["Dylan"]
    louder = _press(dylan, TV_ID, key="volume_up")
    assert louder.status_code == 200, louder.text
    assert louder.json() == {"ok": True, "via": "app", "message": "Louder on Living room."}


def test_home_assistants_remote_carries_arrows_apps_and_volume(hub, home_client, people):
    _two_tvs(hub, home_client)
    _enroll(home_client, Key(), KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    _asleep(hub, KID_TV)
    st = _ha_tv(hub, volume_level=0.3, is_volume_muted=False)
    st["attributes"]["supported_features"] |= 1024 | 4 | 8
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]

    row = _row(dylan, KID_TV)
    assert row["kids"] is True and row["online"] is False  # the app isn't running, the box still takes its remote
    assert {"up", "select", "back", "home", "menu", "play_pause", "channel_up", "volume_up", "mute", "volume_set",
            "power_on", "power_off", "text", "app"} <= set(row["controls"])
    assert row["volume"] == 30 and row["muted"] is False
    assert {"id": "youtube", "name": "YouTube"} in row["apps"]

    assert _press(dylan, KID_TV, key="down").json() == {"ok": True, "via": "ha", "message": "Down on Theo's TV."}
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "DPAD_DOWN"}) in hub.calls
    assert _press(dylan, KID_TV, key="channel_up").status_code == 200
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "CHANNEL_UP"}) in hub.calls
    assert _press(dylan, KID_TV, key="volume_set", volume=45).status_code == 200
    assert ("media_player", "volume_set", {"entity_id": "media_player.theo_tv", "volume_level": 0.45}) in hub.calls
    assert _press(dylan, KID_TV, key="volume_set", volume=400).status_code == 400
    assert _press(dylan, KID_TV, key="app", app="youtube").status_code == 200

    # A child: their own TV, kids' apps only, and nobody else's TV in the list.
    mine = theo.client.get(LIST, headers=theo.h).json()["tvs"]
    assert KID_TV in [row["id"] for row in mine] and BED not in [row["id"] for row in mine]
    kid_row = next(row for row in mine if row["id"] == KID_TV)
    assert all(app["id"] != "youtube" for app in kid_row["apps"])
    assert _press(theo, KID_TV, key="select").status_code == 200
    assert _press(theo, KID_TV, key="app", app="youtube").status_code == 403
    assert _press(theo, BED, key="pause").status_code == 403


def test_a_kids_tv_is_only_tuned_to_a_kids_channel(hub, home_client, people):
    _two_tvs(hub, home_client)
    kid = Key()
    _enroll(home_client, kid, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room")
    _on_screen(home_client, kid, KID_TV)
    dylan = people["phones"]["Dylan"]
    grown = _press(dylan, KID_TV, key="tune", channel_key="xc:thomas-creek:10")
    assert grown.status_code == 403 and grown.json()["message"] == "That channel isn't for this TV"
    assert _press(dylan, KID_TV, key="tune", channel_key="xc:thomas-creek:11").status_code == 200


def test_a_title_is_sent_to_a_tv_and_a_child_sends_only_kids_titles(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    _on_screen(home_client, office)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    sent = _press(dylan, TV_ID, key="send", movie_id="xc:thomas-creek:501", title="Dune")
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"ok": True, "via": "app", "message": "Playing on Living room."}
    queued = hub.tv_app.pending_commands(TV_ID)[-1]
    assert queued["action"] == "play" and queued["kind"] == "movie" and queued["movie_id"] == "xc:thomas-creek:501"
    assert _press(dylan, TV_ID, key="send").status_code == 400
    # A child's phone is held to the kids' list even on a TV that isn't set up as a kids' TV.
    own = Key()
    _enroll(home_client, own, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", room="theo_room", media_entity="media_player.theo_tv")
    _on_screen(home_client, own, KID_TV)
    assert _press(theo, TV_ID, key="send", channel_key="xc:thomas-creek:11").status_code == 403  # not his room
    news = _press(theo, KID_TV, key="send", channel_key="xc:thomas-creek:10")
    assert news.status_code == 403 and news.json()["message"] == "That channel isn't on the kids' list."
    assert _press(theo, KID_TV, key="send", movie_id="xc:thomas-creek:501").status_code == 403
    assert _press(theo, KID_TV, key="send", channel_key="xc:thomas-creek:11").status_code == 200
    assert _press(dylan, KID_TV, key="send", channel_key="xc:thomas-creek:10").status_code == 200


def test_a_tv_and_a_stranger_get_no_remote(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    tv = _auth(_token(home_client, office, Clock())["access_token"])
    assert home_client.get(LIST, headers=tv).status_code == 403
    assert home_client.post(f"{LIST}/{TV_ID}/press", headers=tv, json={"key": "pause"}).status_code == 403
    assert home_client.get(LIST).status_code == 401


def test_a_held_arrow_is_let_through_and_a_stuck_one_is_not(hub, home_client, people, monkeypatch):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    _adb(hub)
    dylan = people["phones"]["Dylan"]
    answers = [_press(dylan, TV_ID, key="down").json() for _ in range(tvpress.MAX_IN_WINDOW + 1)]
    assert all(a["ok"] for a in answers[:-1])
    assert answers[-1] == {"ok": False, "via": None, "message": "That's as fast as it goes."}


def test_the_list_names_the_person_a_tv_plays_for(hub, home_client, people):
    _two_tvs(hub, home_client)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(watcher=people["avery"]["id"])  # an id, as the TV's check-in keeps it
    tvs[BED].update(watcher="per_gone")
    hub.tv_app._save_tvs(tvs)
    dylan = people["phones"]["Dylan"]
    assert _row(dylan, TV_ID)["watching"] == "Avery"
    assert _row(dylan, BED)["watching"] != "per_gone"  # an unknown id is never shown as a name


def test_same_named_tvs_are_told_apart_and_a_long_gone_box_is_left_out(hub, home_client, people):
    _two_tvs(hub, home_client)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(name="Apple TV", room="living_room")
    tvs[BED].update(name="Apple TV", room="living_room")
    hub.tv_app._save_tvs(tvs)
    dylan = people["phones"]["Dylan"]
    page = dylan.client.get(LIST, headers=dylan.h).json()["tvs"]
    assert sorted(row["name"] for row in page if row["id"] in (TV_ID, BED)) == ["Apple TV 1", "Apple TV 2"]

    # Not seen for a month, and nothing the hub could do to it: an old registration.
    tvs = hub.tv_app.tvs()
    old = time.time() - 30 * 86400
    tvs[BED].update(state_at=old, last_seen_at=old, last_config_at=old)
    hub.tv_app._save_tvs(tvs)
    page = dylan.client.get(LIST, headers=dylan.h).json()["tvs"]
    assert BED not in [row["id"] for row in page]
    assert _row(dylan, TV_ID)["name"] == "Apple TV"


def test_a_box_that_does_not_answer_is_offered_no_dead_buttons(hub, home_client, people):
    """Seen on the live hub: a TV that was off still drew arrows and volume because the hub had its address."""
    _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _adb(hub)
    dylan = people["phones"]["Dylan"]
    assert {"up", "power_on", "input"} <= set(_row(dylan, TV_ID)["controls"])  # network debugging answers

    tvpress._adb_mark(hub, TV_ID, False)
    assert not {"up", "select", "menu", "power_on", "input"} & set(_row(dylan, TV_ID)["controls"])
    assert _press(dylan, TV_ID, key="up").status_code == 409
    tvpress._adb_mark(hub, TV_ID, True)

    # A press that finds the box gone takes the buttons away for the next list, and says so plainly.
    calls = _adb(hub)

    async def gone(args, _wait):
        calls.append(args)
        return AdbResult(1, "failed to connect")

    hub.tv_app.adb._run = gone
    tvpress._ready(hub).clear()
    failed = _press(dylan, TV_ID, key="up")
    assert failed.status_code == 409, failed.text
    assert failed.json()["message"] == "Living room didn't answer. It may be off; turn it on and try again."
    assert "up" not in _row(dylan, TV_ID)["controls"]


def test_open_app_without_network_debugging_brings_the_app_forward(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["foreground"] = False
    hub.tv_app._save_tvs(tvs)
    dylan = people["phones"]["Dylan"]
    opened = _press(dylan, TV_ID, key="open_app")
    assert opened.status_code == 200, opened.text
    assert opened.json() == {"ok": True, "via": "app", "message": "Opening Thomas Creek TV on Living room."}


def test_a_film_says_how_old_its_place_is_so_the_bar_keeps_moving(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    _on_screen(home_client, office, playing={"movie_id": "m1", "title": "A film", "kind": "movie",
                                             "position_s": 600, "duration_s": 5400})
    dylan = people["phones"]["Dylan"]
    assert _row(dylan, TV_ID)["now_playing"]["position_age_s"] == 0
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["state_at"] -= 40  # the TV last said where it was 40 seconds ago and is still connected
    tvs[TV_ID]["last_seen_at"] = time.time()
    hub.tv_app._save_tvs(tvs)
    now = _row(dylan, TV_ID)["now_playing"]
    assert now["position_s"] == 600 and 39 <= now["position_age_s"] <= 42
    # A channel has no place to move on from.
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["playing"] = {"channel_key": "xc:thomas-creek:10", "name": "CNN", "kind": "live"}
    hub.tv_app._save_tvs(tvs)
    assert "position_age_s" not in _row(dylan, TV_ID)["now_playing"]
