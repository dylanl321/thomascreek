"""TV API v1: device-key tokens, lineup, guide, and play-on-TV."""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub.routes.home import shape_event
from hub.tvapp import token_message
from hub.tvlineup import LineupError, channel_key

TV_ID = "tv_office_0123456789"
KID_TV = "tv_theo_0123456789ab"
PASSWORD = "s3cret-tv"


class Key:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        raw = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.pub = base64.b64encode(raw).decode()

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self.key.sign(message, ec.ECDSA(hashes.SHA256()))).decode()


class Clock:
    def __init__(self):
        self.ts = int(time.time()) - 30

    def next(self) -> int:
        self.ts += 1
        return self.ts


def _enroll(client, key: Key, install_id: str, name: str) -> None:
    response = client.post("/v1/tv/enroll", json={
        "install_id": install_id, "name": name, "model": "Shield", "public_key": key.pub})
    assert response.status_code == 200, response.text


def _token(client, key: Key, clock: Clock, install_id: str = TV_ID, person_id: str | None = None) -> dict:
    ts = clock.next()
    body = {"install_id": install_id, "ts": ts, "signature": key.sign(token_message(install_id, ts, person_id))}
    if person_id:
        body["person_id"] = person_id
    response = client.post("/v1/tv/token", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_channel_keys_never_come_from_a_url():
    assert channel_key(iptv_org="CNN.us") == "iptv-org:CNN.us"
    assert channel_key(source="thomas-creek", stream_id="44102") == "xc:thomas-creek:44102"
    assert channel_key(source="thomas-creek", tvg_id="cnn.us") == "tvg:thomas-creek:cnn.us"
    assert channel_key(iptv_org="CNN.us", source="thomas-creek", stream_id="1") == "iptv-org:CNN.us"
    for bad in ("http://user:pass@host/live/1", "user:pass", "cnn/us"):
        with pytest.raises(LineupError):
            channel_key(source="thomas-creek", stream_id=bad)


def test_tv_token_is_room_scoped_and_cannot_unlock(hub, home_client, away_client, people):
    key, clock = Key(), Clock()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    issued = _token(home_client, key, clock)
    assert issued["role"] == "tablet" and issued["room"] == "living_room"
    assert "doors" not in issued["capabilities"] and "cameras" not in issued["capabilities"]
    assert "lights.all" not in issued["capabilities"] and "lights.own_room" in issued["capabilities"]
    auth = _auth(issued["access_token"])

    home = home_client.get("/v1/home", headers=auth).json()
    assert {room["id"] for room in home["rooms"]} == {"living_room"}
    assert home["cameras"] == []
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "light.set", "target": "light.living_room", "params": {"on": True}}).status_code == 200
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "light.set", "target": "light.kitchen", "params": {"on": True}}).status_code == 403
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "scene.run", "target": "scene.movie_night"}).status_code == 200
    for action, target, params in (
        ("lock.set", "lock.front_door", {"locked": False}),
        ("garage.set", "cover.large_garage", {"open": True}),
        ("lock.set", "lock.front_door", {"locked": True}),
    ):
        denied = home_client.post("/v1/actions", headers=auth, json={"action": action, "target": target, "params": params})
        assert denied.status_code == 403, denied.text
    hub.home.states["switch.garage_door_toggle"] = {
        "entity_id": "switch.garage_door_toggle", "state": "off", "attributes": {"friendly_name": "Garage door toggle"},
        "last_updated": "2026-11-12T01:00:00+00:00"}
    hub.home.entity_area["switch.garage_door_toggle"] = "living_room"
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "switch.set", "target": "switch.garage_door_toggle", "params": {"on": True}}).status_code == 403
    assert home_client.get("/v1/cameras", headers=auth).status_code == 403
    assert home_client.get("/v1/tvapp", headers=auth).status_code == 403
    assert away_client.get("/v1/home", headers=auth).status_code == 403

    same = clock.ts
    replay = home_client.post("/v1/tv/token", json={
        "install_id": TV_ID, "ts": same, "signature": key.sign(token_message(TV_ID, same, None))})
    assert replay.status_code == 401 and replay.json()["error"] == "replay"
    stale = int(time.time()) - 3600
    assert home_client.post("/v1/tv/token", json={
        "install_id": TV_ID, "ts": stale, "signature": key.sign(token_message(TV_ID, stale, None))}).status_code == 400

    hub.tv_app.set_tv(TV_ID, status="revoked")
    assert home_client.get("/v1/home", headers=auth).status_code == 401


def test_kids_profile_cannot_be_widened(hub, home_client, people):
    key, clock = Key(), Clock()
    _enroll(home_client, key, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    dylan = people["dylan"]["id"]
    grown = home_client.post("/v1/tv/token", json={
        "install_id": KID_TV, "ts": clock.next(), "person_id": dylan,
        "signature": key.sign(token_message(KID_TV, clock.ts, dylan))})
    assert grown.status_code == 403

    hub.db.update("people", "id", people["theo"]["id"], {"grants": [
        {"capability": "cameras"}, {"capability": "doors"}, {"action": "lock.set"}]})
    issued = _token(home_client, key, clock, KID_TV, people["theo"]["id"])
    assert issued["role"] == "child"
    assert "doors" not in issued["capabilities"] and "cameras" not in issued["capabilities"]
    auth = _auth(issued["access_token"])
    me = home_client.get("/v1/me", headers=auth).json()
    assert me["role"] == "child"
    assert home_client.get("/v1/cameras", headers=auth).status_code == 403
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "lock.set", "target": "lock.front_door", "params": {"locked": False}}).status_code == 403
    assert home_client.post("/v1/cameras/driveway/pass", headers=auth, json={}).status_code == 403
    rooms = {room["id"] for room in home_client.get("/v1/home", headers=auth).json()["rooms"]}
    assert rooms == {"theo_room"}
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "light.set", "target": "light.theo_room", "params": {"on": True}}).status_code == 200
    assert home_client.post("/v1/actions", headers=auth, json={
        "action": "light.set", "target": "light.living_room", "params": {"on": True}}).status_code == 403


def test_config_etag_changes_only_when_the_config_changes(hub, home_client):
    key, clock = Key(), Clock()
    _enroll(home_client, key, TV_ID, "Office")
    pending = _config(home_client, key, clock.next())
    assert pending.json() == {"status": "pending"}
    hub.vault.set("iptv.xtream_server", "http://iptv.example:8080")
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", PASSWORD)
    hub.tv_app.set_tv(TV_ID, status="approved", profile="adult", room="living_room")
    first = _config(home_client, key, clock.next())
    assert first.status_code == 200
    body = first.json()
    assert body["version"] >= 1 and body["profile"] == "adult" and body["room"] == "living_room"
    assert body["lineup_version"] == 0 and body["theme"] is None
    assert "updated_at" not in body
    tag = first.headers["etag"]
    again = _config(home_client, key, clock.next(), {"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    hub.tv_app.set_kids_categories(["Kids"])
    changed = _config(home_client, key, clock.next(), {"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag


def test_lineup_etag_kids_and_no_credentials(hub, home_client, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    bad = dylan.client.put("/v1/tvapp/lineup", headers=dylan.h, json={"channels": [{
        "name": "Secret", "source": "thomas-creek", "stream_id": "1",
        "logo": f"http://user:{PASSWORD}@cdn.example/a.png"}]})
    assert bad.status_code == 400 and PASSWORD not in bad.text
    saved = dylan.client.put("/v1/tvapp/lineup", headers=dylan.h, json={"channels": [
        {"name": "CNN", "number": 20, "group": "News", "source": "thomas-creek", "stream_id": "10",
         "logo": "https://img.example/cnn.png", "kids": False},
        {"name": "Cartoon", "number": 3, "group": "Kids", "source": "thomas-creek", "stream_id": "11", "kids": True},
        {"name": "Hidden", "source": "thomas-creek", "stream_id": "12", "hidden": True, "kids": True},
    ]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["hidden"] == 1 and all(row["key"].startswith("xc:thomas-creek:") for row in saved.json()["channels"])
    assert people["phones"]["Avery"].client.put("/v1/tvapp/lineup", headers=people["phones"]["Avery"].h,
                                                json={"channels": []}).status_code == 403

    first = dylan.client.get("/v1/tv/lineup", headers=dylan.h)
    assert first.status_code == 200
    assert [row["name"] for row in first.json()["channels"]] == ["Cartoon", "CNN"]
    tag = first.headers["etag"]
    assert dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "If-None-Match": tag}).status_code == 304
    kid = theo.client.get("/v1/tv/lineup", headers=theo.h).json()
    assert [row["name"] for row in kid["channels"]] == ["Cartoon"]
    assert all(row["kids"] for row in kid["channels"])


def test_state_publishes_what_is_on(hub, home_client, people):
    key, clock = Key(), Clock()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room", media_entity="media_player.living")
    auth = _auth(_token(home_client, key, clock)["access_token"])
    dylan = hub.subject_for(hub.db.device(people["phones"]["Dylan"].device_id), True)
    theo = hub.subject_for(hub.db.device(people["phones"]["Theo"].device_id), True)
    phones = hub.bus.subscribe(lambda ev: shape_event(hub, dylan, ev), dylan.person_id)
    child = hub.bus.subscribe(lambda ev: shape_event(hub, theo, ev), theo.person_id)
    response = home_client.post("/v1/tv/state", headers=auth, json={
        "foreground": True,
        "playing": {"channel_key": "xc:thomas-creek:10", "title": "Newsroom", "name": "CNN"},
        "health": {"app_version": "1.4.1", "last_error": f"timeout http://user:{PASSWORD}@cdn.example/a"},
    })
    assert response.status_code == 200, response.text
    event = phones.queue.get_nowait()
    assert event["type"] == "tv" and event["data"]["playing"]["title"] == "Newsroom"
    assert event["data"]["room"] == "living_room"
    assert child.queue.empty()
    overview = people["phones"]["Dylan"].client.get("/v1/tvapp", headers=people["phones"]["Dylan"].h).text
    assert PASSWORD not in overview and "[url]" in overview


def test_play_delivers_to_the_tv_and_reports_back(hub, home_client, people):
    hub.config.hub.live_actions = ["tv.play", "tv.stop"]
    office, kid_key = Key(), Key()
    clock = Clock()
    _enroll(home_client, office, TV_ID, "Living room")
    _enroll(home_client, kid_key, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    hub.tv_app.set_lineup([
        {"name": "CNN", "source": "thomas-creek", "stream_id": "10", "kids": False},
        {"name": "Cartoon", "source": "thomas-creek", "stream_id": "11", "kids": True},
    ])
    news, toon = "xc:thomas-creek:10", "xc:thomas-creek:11"
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    tv_sub = hub.bus.subscribe(lambda ev: ev if ev.get("type") == "tv_command" else None, None, device_id=f"tv:{TV_ID}", tv=True)
    phone_sub = hub.bus.subscribe(lambda ev: ev if ev.get("type") == "tv_command" else None, dylan.device_id)

    sent = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID, "params": {"channel_key": news, "title": "Newsroom", "resume": 0}})
    assert sent.status_code == 200, sent.text
    assert sent.json()["command_id"].startswith("tvc_") and sent.json()["dry_run"] is False
    command = tv_sub.queue.get_nowait()["data"]
    assert command["kind"] == "live" and command["channel_key"] == news and command["title"] == "Newsroom"
    assert "://" not in str(command) and phone_sub.queue.empty()
    polled = home_client.get("/v1/tv/commands", headers=_auth(_token(home_client, office, clock)["access_token"]))
    assert polled.json()["commands"][0]["id"] == command["id"]
    ack = home_client.post("/v1/tv/commands/ack", headers=_auth(_token(home_client, office, clock)["access_token"]),
                           json={"ids": [command["id"]]})
    assert ack.json()["acked"] == 1
    assert home_client.get("/v1/tv/commands?timeout=0", headers=_auth(_token(home_client, office, clock)["access_token"])).json()["commands"] == []

    assert theo.client.post("/v1/actions", headers=theo.h, json={
        "action": "tv.play", "target": TV_ID, "params": {"channel_key": toon}}).status_code == 403
    assert theo.client.post("/v1/actions", headers=theo.h, json={
        "action": "tv.play", "target": KID_TV, "params": {"channel_key": news}}).status_code == 403
    ok = theo.client.post("/v1/actions", headers=theo.h, json={
        "action": "tv.play", "target": KID_TV, "params": {"channel_key": toon}})
    assert ok.status_code == 200, ok.text
    stopped = dylan.client.post("/v1/actions", headers=dylan.h, json={"action": "tv.stop", "target": TV_ID})
    assert stopped.status_code == 200 and stopped.json()["command_id"]

    auth = _auth(_token(home_client, office, clock)["access_token"])
    watched = hub.bus.subscribe(lambda ev: shape_event(hub, hub.subject_for(hub.db.device(dylan.device_id), True), ev),
                                people["dylan"]["id"])
    reported = home_client.post("/v1/tv/state", headers=auth, json={
        "command_id": sent.json()["command_id"], "command_ok": False,
        "command_error": f"no signal http://user:{PASSWORD}@x",
        "playing": None,
    })
    assert reported.status_code == 200, reported.text
    event = watched.queue.get_nowait()
    assert event["data"]["command"]["ok"] is False
    assert PASSWORD not in str(event) and "[url]" in event["data"]["command"]["error"]
    assert dylan.client.get("/v1/tv/commands", headers=dylan.h).status_code == 403


def test_players_follow_who_can_play(hub, home_client, people):
    office, kid_key = Key(), Key()
    clock = Clock()
    _enroll(home_client, office, TV_ID, "Living room")
    _enroll(home_client, kid_key, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    before = dylan.client.get("/v1/tv/players", headers=dylan.h).json()["tvs"]
    assert {row["id"] for row in before} == {TV_ID, KID_TV}
    aged = hub.tv_app.tvs()
    aged[TV_ID]["last_seen_at"] = time.time() - 1000
    aged[TV_ID].pop("state_at", None)
    hub.tv_app._save_tvs(aged)
    quiet = {row["id"]: row for row in dylan.client.get("/v1/tv/players", headers=dylan.h).json()["tvs"]}
    assert quiet[TV_ID]["online"] is False and quiet[KID_TV]["online"] is True
    auth = _auth(_token(home_client, kid_key, clock, KID_TV)["access_token"])
    home_client.post("/v1/tv/state", headers=auth, json={"playing": {"name": "Cartoon", "title": "Blue"}})
    after = {row["id"]: row for row in dylan.client.get("/v1/tv/players", headers=dylan.h).json()["tvs"]}
    assert after[KID_TV]["online"] is True and after[KID_TV]["playing"]["title"] == "Blue"
    assert after[KID_TV]["room_name"] == "Theo's room"
    kid_rows = theo.client.get("/v1/tv/players", headers=theo.h).json()["tvs"]
    assert [row["id"] for row in kid_rows] == [KID_TV]


def _config(client, key: Key, ts: int, headers: dict | None = None):
    return client.post("/v1/tv/config", headers=headers, json={
        "install_id": TV_ID, "ts": ts, "signature": key.sign(f"tvconfig:{TV_ID}:{ts}".encode())})


def _xt(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _library(hub):
    now = datetime.now(timezone.utc)
    hub.tv_app.set_kids_categories(["Kids"])
    xml = f"""<?xml version="1.0"?>
    <tv>
      <programme start="{_xt(now - timedelta(minutes=30))}" stop="{_xt(now + timedelta(minutes=30))}" channel="cnn.us">
        <title>Newsroom</title><desc>The day.</desc><icon src="https://img.example/news.jpg"/>
      </programme>
      <programme start="{_xt(now + timedelta(minutes=30))}" stop="{_xt(now + timedelta(hours=1))}" channel="cnn.us">
        <title>Tonight</title>
      </programme>
      <programme start="{_xt(now - timedelta(hours=3))}" stop="{_xt(now - timedelta(hours=2))}" channel="toon.us">
        <title>Old cartoon</title>
      </programme>
      <programme start="{_xt(now - timedelta(minutes=10))}" stop="{_xt(now + timedelta(minutes=10))}" channel="toon.us">
        <title>Blue</title>
      </programme>
    </tv>""".encode()
    hub.tv_catalog.ingest(
        live=[
            {"stream_id": 10, "name": "CNN", "num": 20, "category_id": "1", "stream_icon": "https://img.example/cnn.png",
             "epg_channel_id": "cnn.us", "tv_archive": 1, "tv_archive_duration": 2},
            {"stream_id": 11, "name": "Cartoon", "num": 3, "category_id": "2",
             "stream_icon": f"http://user:{PASSWORD}@img.example/c.png", "epg_channel_id": "toon.us", "tv_archive": 0},
        ],
        live_categories=[{"category_id": "1", "category_name": "News"}, {"category_id": "2", "category_name": "Kids"}],
        vod=[
            {"stream_id": 100, "name": "Drama Movie", "category_id": "3", "stream_icon": "https://img.example/drama.png"},
            {"stream_id": 101, "name": "Kids Movie", "category_id": "4", "stream_icon": "https://img.example/kids.png"},
        ],
        vod_categories=[{"category_id": "3", "category_name": "Drama"}, {"category_id": "4", "category_name": "Kids"}],
        series=[
            {"series_id": 50, "name": "Cartoon Show", "category_id": "5", "cover": "https://img.example/show.png"},
            {"series_id": 51, "name": "Drama Show", "category_id": "6", "cover": "https://img.example/dshow.png"},
        ],
        series_categories=[{"category_id": "5", "category_name": "Kids"}, {"category_id": "6", "category_name": "Drama"}],
        xmltv=xml,
    )
    hub.tv_catalog.store_series({
        "info": {"name": "Cartoon Show", "plot": "Fun", "cover": "https://img.example/show.png"},
        "episodes": {"1": [{"id": "900", "episode_num": 1, "title": "Pilot", "info": {
            "plot": "Starts", "duration_secs": 1200, "movie_image": "https://img.example/pilot.jpg"}}]},
    }, "series:thomas-creek:50")
    hub.tv_catalog.store_movie({
        "info": {"name": "Kids Movie", "plot": "A film", "movie_image": "https://img.example/kids.png",
                 "duration_secs": 5000, "releasedate": "2020"},
    }, "vod:thomas-creek:101")


def test_guide_series_movies_and_search_are_filtered(hub, people):
    _library(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    guide = dylan.client.get("/v1/tv/guide", headers=dylan.h)
    assert guide.status_code == 200, guide.text
    assert PASSWORD not in guide.text
    by_name = {row["name"]: row for row in guide.json()["channels"]}
    assert by_name["CNN"]["now"]["title"] == "Newsroom" and by_name["CNN"]["now"]["catchup"] is True
    assert by_name["CNN"]["next"]["title"] == "Tonight" and by_name["CNN"]["next"]["catchup"] is False
    assert by_name["Cartoon"]["logo"] is None
    kid = theo.client.get("/v1/tv/guide", headers=theo.h).json()
    assert [row["name"] for row in kid["channels"]] == ["Cartoon"]

    by_channel = dylan.client.get("/v1/tv/library/search", headers=dylan.h, params={"q": "cnn"}).json()
    assert any(row["name"] == "CNN" for row in by_channel["channels"])
    by_show = dylan.client.get("/v1/tv/library/search", headers=dylan.h, params={"q": "news"}).json()
    assert any(row["title"] == "Newsroom" for row in by_show["programmes"])
    assert theo.client.get("/v1/tv/library/search", headers=theo.h, params={"q": "drama"}).json()["movies"] == []
    films = theo.client.get("/v1/tv/movies", headers=theo.h).json()
    assert [row["name"] for row in films["movies"]] == ["Kids Movie"]
    assert theo.client.get("/v1/tv/movies/vod:thomas-creek:100", headers=theo.h).status_code == 404
    detail = dylan.client.get("/v1/tv/movies/vod:thomas-creek:101", headers=dylan.h)
    assert detail.status_code == 200 and detail.json()["plot"] == "A film" and PASSWORD not in detail.text
    shows = theo.client.get("/v1/tv/series", headers=theo.h).json()
    assert [row["name"] for row in shows["series"]] == ["Cartoon Show"]
    one = dylan.client.get("/v1/tv/series/series:thomas-creek:50", headers=dylan.h).json()
    assert one["seasons"][0]["episodes"][0]["id"] == "ep:thomas-creek:900"
    assert theo.client.get("/v1/tv/series/series:thomas-creek:51", headers=theo.h).status_code == 404
    assert dylan.client.get("/v1/tv/library/search", headers=dylan.h, params={"q": "n"}).status_code == 400


def test_play_catchup_movie_and_episode(hub, people):
    hub.config.hub.live_actions = ["tv.play"]
    _library(hub)
    office = Key()
    _enroll(people["phones"]["Dylan"].client, office, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    hub.tv_app.set_tv  # keep the attribute lookup honest
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    start = time.time() - 600
    sent = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID,
        "params": {"kind": "catchup", "channel_key": "xc:thomas-creek:10", "start": start, "resume": 30}})
    assert sent.status_code == 200, sent.text
    command = hub.tv_app.pending_commands(TV_ID)[-1]
    assert command["kind"] == "catchup" and command["resume"] == 30 and command["channel_key"] == "xc:thomas-creek:10"
    assert "start" in command and PASSWORD not in str(command)

    movie = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID, "params": {"movie_id": "vod:thomas-creek:101"}})
    assert movie.status_code == 200, movie.text
    assert hub.tv_app.pending_commands(TV_ID)[-1]["kind"] == "movie"
    assert theo.client.post("/v1/actions", headers=theo.h, json={
        "action": "tv.play", "target": TV_ID,
        "params": {"movie_id": "vod:thomas-creek:100"}}).status_code == 403

    episode = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID,
        "params": {"episode_id": "ep:thomas-creek:900", "series_id": "series:thomas-creek:50", "resume": 15}})
    assert episode.status_code == 200, episode.text
    last = hub.tv_app.pending_commands(TV_ID)[-1]
    assert last["kind"] == "episode" and last["episode_id"] == "ep:thomas-creek:900" and last["resume"] == 15
    mismatch = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID,
        "params": {"episode_id": "ep:thomas-creek:900", "series_id": "series:thomas-creek:51"}})
    assert mismatch.status_code == 400


@respx.mock
def test_refresh_reads_the_provider_without_echoing_the_password(hub, people):
    hub.vault.set("iptv.xtream_server", "http://iptv.example:8080")
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", PASSWORD)
    lists = {
        "get_live_streams": [{"stream_id": 10, "name": "CNN", "num": 1, "category_id": "1",
                              "stream_icon": "https://img.example/cnn.png", "epg_channel_id": "cnn.us",
                              "tv_archive": 1, "tv_archive_duration": 1}],
        "get_live_categories": [{"category_id": "1", "category_name": "News"}],
        "get_vod_streams": [], "get_vod_categories": [], "get_series": [], "get_series_categories": [],
    }

    def answer(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if action:
            return httpx.Response(200, json=lists[action])
        return httpx.Response(200, content=b"<tv></tv>")

    respx.get("http://iptv.example:8080/player_api.php").mock(side_effect=answer)
    respx.get("http://iptv.example:8080/xmltv.php").mock(side_effect=answer)
    dylan = people["phones"]["Dylan"]
    refreshed = dylan.client.post("/v1/tvapp/catalog/refresh", headers=dylan.h)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["channels"] == 1 and PASSWORD not in refreshed.text
    lineup = dylan.client.get("/v1/tv/lineup", headers=dylan.h)
    assert lineup.json()["channels"][0]["key"] == "xc:thomas-creek:10"
    assert PASSWORD not in lineup.text
