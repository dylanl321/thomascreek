"""Move what's playing to another room, from the same second."""

from __future__ import annotations

from tests.test_tv_api import KID_TV, TV_ID, Clock, Key, _auth, _enroll, _token

BED = "tv_bed_0123456789abcd"


def _two_tvs(hub, home_client):
    office, bed = Key(), Key()
    _enroll(home_client, office, TV_ID, "Living room")
    _enroll(home_client, bed, BED, "Bedroom")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room", media_entity="media_player.living")
    hub.tv_app.set_tv(BED, status="approved", room="bedroom")
    hub.tv_app.set_lineup([
        {"name": "CNN", "source": "thomas-creek", "stream_id": "10", "kids": False},
        {"name": "Cartoon", "source": "thomas-creek", "stream_id": "11", "kids": True},
    ])
    hub.config.hub.live_actions = ["tv.play", "tv.stop"]
    return office, bed


def test_position_is_kept_and_devices_list_the_tvs(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    clock = Clock()
    auth = _auth(_token(home_client, office, clock)["access_token"])
    reported = home_client.post("/v1/tv/state", headers=auth, json={
        "playing": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom", "name": "CNN", "position_s": 87.4,
                    "movie_id": "not a url"},
    })
    assert reported.status_code == 200, reported.text
    dylan = people["phones"]["Dylan"]
    devices = dylan.client.get("/v1/tv/devices", headers=dylan.h)
    assert devices.status_code == 200, devices.text
    by_id = {row["id"]: row for row in devices.json()["devices"]}
    assert set(by_id) == {TV_ID, BED}
    assert by_id[TV_ID]["playing"]["position_s"] == 87
    assert by_id[TV_ID]["playing"]["title"] == "Newsroom"
    assert "movie_id" not in by_id[TV_ID]["playing"]
    assert by_id[TV_ID]["media_entity"] == "media_player.living"
    assert by_id[TV_ID]["room"] == "living_room" and by_id[TV_ID]["online"] is True
    players = dylan.client.get("/v1/tv/players", headers=dylan.h).json()["tvs"]
    living = next(row for row in players if row["id"] == TV_ID)
    assert living["playing"]["position_s"] == 87
    bad = home_client.post("/v1/tv/state", headers=auth, json={"playing": {"title": "Newsroom", "position_s": 999999}})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_request"


def test_handoff_resumes_on_the_other_tv_then_stops_the_first(hub, home_client, people):
    office, bed = _two_tvs(hub, home_client)
    clock = Clock()
    auth = _auth(_token(home_client, office, clock)["access_token"])
    home_client.post("/v1/tv/state", headers=auth, json={
        "playing": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom", "name": "CNN", "position_s": 125},
    })
    dylan = people["phones"]["Dylan"]
    moved = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": TV_ID, "to_device": BED})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["ok"] is True and body["moved"] is True and body["from"] == TV_ID and body["to"] == BED
    assert "Newsroom" in body["message"] and "Bedroom" in body["message"]
    play = hub.tv_app.pending_commands(BED)[0]
    stop = hub.tv_app.pending_commands(TV_ID)[0]
    assert play["action"] == "play" and play["channel_key"] == "xc:thomas-creek:10" and play["resume"] == 125
    assert play["kind"] == "live" and "://" not in str(play)
    assert stop["action"] == "stop"
    assert body["command_id"] == play["id"]

    again = dylan.client.get("/v1/tv/devices", headers=dylan.h).json()["devices"]
    assert again[0]["id"] == BED and again[0]["usual"] is True

    quiet = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": BED, "to_device": BED})
    assert quiet.status_code == 200 and quiet.json()["ok"] is False and "already" in quiet.json()["message"]

    home_client.post("/v1/tv/state", headers=_auth(_token(home_client, bed, clock, BED)["access_token"]),
                     json={"playing": None})
    empty = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": BED, "to_device": TV_ID})
    assert empty.status_code == 200 and empty.json() == {
        "ok": False, "moved": False, "message": "Nothing is playing on Bedroom", "from": BED, "to": TV_ID}
    assert [row["action"] for row in hub.tv_app.pending_commands(TV_ID)] == ["stop"]

    by_player = dylan.client.post("/v1/tv/handoff", headers=dylan.h,
                                  json={"from_device": "media_player.living", "to_device": "no-such-tv"})
    assert by_player.status_code == 404 and by_player.json()["message"]


def test_title_only_heartbeat_still_moves_the_channel_the_hub_started(hub, home_client, people):
    _two_tvs(hub, home_client)
    dylan = people["phones"]["Dylan"]
    sent = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID, "params": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}})
    assert sent.status_code == 200, sent.text
    hub.tv_app.record_state(TV_ID, {"playing": {"title": "Newsroom", "name": "CNN", "position_s": 40}})
    moved = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": TV_ID, "to_device": BED})
    assert moved.status_code == 200, moved.text
    play = next(row for row in hub.tv_app.pending_commands(BED) if row["action"] == "play")
    assert play["channel_key"] == "xc:thomas-creek:10" and play["resume"] == 40 and play["title"] == "Newsroom"


def test_a_movie_resumes_and_a_kids_room_refuses_the_grown_up_one(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    _enroll(home_client, Key(), KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 100, "name": "Drama Movie", "category_id": "3"},
        {"stream_id": 101, "name": "Kids Movie", "category_id": "4"},
    ], vod_categories=[
        {"category_id": "3", "category_name": "Drama"},
        {"category_id": "4", "category_name": "Kids"},
    ])
    clock = Clock()
    auth = _auth(_token(home_client, office, clock)["access_token"])
    home_client.post("/v1/tv/state", headers=auth, json={
        "playing": {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "position_s": 600, "kind": "movie"},
    })
    dylan = people["phones"]["Dylan"]
    moved = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": TV_ID, "to_device": KID_TV})
    assert moved.status_code == 200, moved.text
    play = hub.tv_app.pending_commands(KID_TV)[0]
    assert play["kind"] == "movie" and play["movie_id"] == "vod:thomas-creek:101" and play["resume"] == 600

    home_client.post("/v1/tv/state", headers=auth, json={
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 10},
    })
    refused = dylan.client.post("/v1/tv/handoff", headers=dylan.h, json={"from_device": TV_ID, "to_device": KID_TV})
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == "Theo's TV only plays kids' shows"
    assert "Traceback" not in refused.text

    theo = people["phones"]["Theo"]
    blocked = theo.client.post("/v1/tv/handoff", headers=theo.h, json={"from_device": TV_ID, "to_device": KID_TV})
    assert blocked.status_code == 403
    assert blocked.json()["message"] == "That room isn't one you can use"
    kid_rows = theo.client.get("/v1/tv/devices", headers=theo.h).json()["devices"]
    assert [row["id"] for row in kid_rows] == [KID_TV]
