"""The phone is the remote. A tap reaches that TV at once."""

from __future__ import annotations

from hub.artwork import programme_key
from hub.routes.home import shape_event

from tests.test_tv_handoff import BED, _two_tvs
from tests.test_tv_api import KID_TV, TV_ID, Clock, Key, _auth, _enroll, _token


def _playing(home_client, key, channel="xc:thomas-creek:10", title="Newsroom"):
    auth = _auth(_token(home_client, key, Clock())["access_token"])
    reported = home_client.post("/v1/tv/state", headers=auth, json={
        "playing": {"channel_key": channel, "title": title, "name": "CNN", "position_s": 40},
    })
    assert reported.status_code == 200, reported.text


def test_a_button_reaches_only_that_tv(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    _playing(home_client, office)
    dylan = people["phones"]["Dylan"]
    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    phone = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id, subject.device_id)
    living = hub.bus.subscribe(lambda ev: shape_event(hub, hub.tv_app.subject_for(hub.tv_app.tvs()[TV_ID], TV_ID, None), ev),
                                None, device_id=f"tv:{TV_ID}", tv=True)
    other = hub.bus.subscribe(lambda ev: shape_event(hub, hub.tv_app.subject_for(hub.tv_app.tvs()[BED], BED, None), ev),
                               None, device_id=f"tv:{BED}", tv=True)

    sent = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "pause"})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["ok"] is True and body["id"].startswith("tvr_")
    event = living.queue.get_nowait()
    assert event["type"] == "tv_remote"
    assert event["data"]["command"] == "pause" and event["data"]["install_id"] == TV_ID
    assert event["data"]["id"] == body["id"]
    assert other.queue.empty() and phone.queue.empty()
    assert hub.tv_app.pending_commands(TV_ID) == []

    seek = dylan.client.post("/v1/tv/remote", headers=dylan.h,
                             json={"device": "media_player.living", "command": "seek", "position_s": 125.4})
    assert seek.status_code == 200, seek.text
    assert living.queue.get_nowait()["data"]["position_s"] == 125

    missing = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "seek"})
    assert missing.status_code == 400 and missing.json()["message"] == "Say where to skip to."
    far = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "seek", "position_s": 999999})
    assert far.status_code == 400 and far.json()["error"] == "bad_request"
    unknown = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "power"})
    assert unknown.status_code == 400 and unknown.json()["message"] == "That button isn't on this remote."


def test_artwork_search_and_the_room_it_already_knows(hub, home_client, people):
    office, _bed = _two_tvs(hub, home_client)
    _playing(home_client, office)
    dylan = people["phones"]["Dylan"]
    rows = dylan.client.get("/v1/tv/devices", headers=dylan.h).json()["devices"]
    living = next(row for row in rows if row["id"] == TV_ID)
    assert living["playing"]["poster_url"] == f"/v1/art/poster/{programme_key('Newsroom')}"

    hub.tv_app.record_state(TV_ID, {"playing": {"channel_key": "xc:thomas-creek:10", "name": "CNN"}})
    logo = dylan.client.get("/v1/tv/devices", headers=dylan.h).json()["devices"]
    assert next(row for row in logo if row["id"] == TV_ID)["playing"]["poster_url"] == "/v1/art/logo/xc:thomas-creek:10"

    hub.tv_app.record_state(TV_ID, {"playing": None})
    hub.tv_app.record_state(BED, {"playing": {"channel_key": "xc:thomas-creek:11", "title": "Cartoon", "name": "Cartoon"}})
    guessed = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"command": "channel_up"})
    assert guessed.status_code == 200 and guessed.json()["ok"] is True
    bed = hub.bus.subscribe(lambda ev: ev if ev.get("type") == "tv_remote" else None, None, device_id=f"tv:{BED}", tv=True)
    typed = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"command": "text", "text": "  Bluey  "})
    assert typed.json()["ok"] is True
    assert bed.queue.get_nowait()["data"]["text"] == "Bluey"
    junk = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": BED, "command": "text", "text": "http://example"})
    assert junk.status_code == 400 and junk.json()["message"] == "Say what to type."

    hub.tv_app.record_state(TV_ID, {"playing": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}})
    hub.tv_app.record_state(BED, {"playing": {"channel_key": "xc:thomas-creek:11", "title": "Cartoon"}})
    ambiguous = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"command": "pause"})
    assert ambiguous.status_code == 200 and ambiguous.json() == {"ok": False, "message": "Pick a room."}


def test_kids_channel_and_a_room_they_cannot_use(hub, home_client, people):
    _two_tvs(hub, home_client)
    _enroll(home_client, Key(), KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    dylan = people["phones"]["Dylan"]
    theo = people["phones"]["Theo"]
    blocked = theo.client.post("/v1/tv/remote", headers=theo.h, json={"device": TV_ID, "command": "pause"})
    assert blocked.status_code == 403 and blocked.json()["message"] == "That room isn't one you can use"
    grown = dylan.client.post("/v1/tv/remote", headers=dylan.h,
                              json={"device": KID_TV, "command": "tune", "channel_key": "xc:thomas-creek:10"})
    assert grown.status_code == 403 and grown.json()["message"] == "That channel isn't for this TV"
    ok = dylan.client.post("/v1/tv/remote", headers=dylan.h,
                           json={"device": KID_TV, "command": "tune", "channel_key": "xc:thomas-creek:11"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    missing = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "tune"})
    assert missing.status_code == 400 and missing.json()["message"] == "Say which channel."
    gone = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": "no-such-tv", "command": "home"})
    assert gone.status_code == 404 and gone.json()["message"] == "That TV isn't registered"


def test_a_stuck_button_does_not_flood_the_tv(hub, home_client, people):
    _two_tvs(hub, home_client)
    dylan = people["phones"]["Dylan"]
    living = hub.bus.subscribe(lambda ev: ev if ev.get("type") == "tv_remote" else None, None, device_id=f"tv:{TV_ID}", tv=True)
    last = None
    for _ in range(13):
        last = dylan.client.post("/v1/tv/remote", headers=dylan.h, json={"device": TV_ID, "command": "back"})
    assert last is not None and last.status_code == 200
    assert last.json() == {"ok": False, "message": "That's as fast as it goes."}
    assert living.queue.qsize() == 12
