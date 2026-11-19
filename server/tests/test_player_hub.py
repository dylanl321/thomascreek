"""What the new player reads from the hub: the `source` of a play answer, who holds the logins when none is
free, the player's own settings and the Stats report."""

from __future__ import annotations

import json

from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import SERVER, _login

FILM = {"movie_id": "vod:thomas-creek:101", "player": "exo"}


def _play(phone, **extra):
    return phone.client.post("/v1/tv/phone/play", headers=phone.h, json={**FILM, **extra})


def test_a_play_answer_says_where_the_stream_comes_from(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    direct = _play(dylan, direct=True)
    assert direct.status_code == 200, direct.text
    source = direct.json()["source"]
    assert source["route"] == "direct" and source["provider"] == "Thomas Creek"
    assert (source["login"], source["logins"], source["address"]) == (1, 1, 1) and source["addresses"] >= 1
    # Numbers and a name: never the address or the login.
    text = json.dumps(source)
    assert SERVER not in text and PASSWORD not in text and "household" not in text

    relayed = _play(dylan)
    assert relayed.status_code == 200, relayed.text
    assert relayed.json()["source"]["route"] == "relay"
    assert SERVER not in relayed.text and PASSWORD not in relayed.text


def test_a_busy_answer_names_who_holds_the_logins(hub, people):
    _library(hub)
    _login(hub)
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    assert _play(dylan, direct=True).status_code == 200
    busy = _play(avery, direct=True)
    assert busy.status_code == 503 and busy.json()["error"] == "busy"
    assert busy.json()["holders"] == [{"kind": "phone", "name": dylan.name, "title": "Kids Movie"}]
    relayed = _play(avery)
    assert relayed.status_code == 503 and relayed.json()["holders"][0]["name"] == dylan.name
    assert SERVER not in busy.text and PASSWORD not in busy.text and "household" not in busy.text


def test_a_child_never_reads_an_adult_title_in_a_busy_answer(hub, people):
    _library(hub)
    _login(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    grown = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                              json={"movie_id": "vod:thomas-creek:100", "player": "exo", "direct": True})
    assert grown.status_code == 200, grown.text
    busy = _play(theo)
    assert busy.status_code == 503, busy.text
    assert busy.json()["holders"] == [{"kind": "phone", "name": dylan.name}]
    assert "Drama Movie" not in busy.text
    # A kids' title someone else is watching is named for a child too.
    assert _play(dylan, direct=True).status_code == 200
    assert _play(theo).json()["holders"] == [{"kind": "phone", "name": dylan.name, "title": "Kids Movie"}]


def test_player_settings_follow_the_person(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = home_client.patch("/v1/me/settings", headers=dylan.h, json={"changes": {
        "app.player.autoplay": False, "app.player.subtitle_lang": "en", "app.player.subtitle_size": "large",
        "app.player.still_watching": 3}})
    assert r.status_code == 200, r.text
    got = home_client.get("/v1/me/settings", headers=dylan.h).json()["settings"]
    assert got["app.player.autoplay"] is False and got["app.player.subtitle_lang"] == "en"
    assert got["app.player.subtitle_size"] == "large" and got["app.player.still_watching"] == 3
    for bad in ({"app.player.subtitle_size": "huge"}, {"app.player.autoplay": "yes"},
                {"app.player.subtitle_lang": "English"}, {"app.player.still_watching": 99}):
        no = home_client.patch("/v1/me/settings", headers=dylan.h, json={"changes": bad})
        assert no.status_code == 400 and no.json()["error"] == "bad_setting", no.text
    # null clears a choice back to the app's default.
    home_client.patch("/v1/me/settings", headers=dylan.h, json={"changes": {"app.player.subtitle_lang": None}})
    after = home_client.get("/v1/me/settings", headers=dylan.h).json()["settings"]
    assert after.get("app.player.subtitle_lang") is None


def test_stats_report_is_kept_without_addresses_and_only_from_grown_ups(hub, people):
    _library(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    stats = {"route": "direct", "video": "H.264 1920x1080", "bitrate_kbps": 6100, "buffer_s": 41.23456,
             "dropped": 2, "hardware": True, "url": f"{SERVER}/movie/household/{PASSWORD}/101.mp4",
             "who": "household@iptv", "Bad Key": 1, "nested": {"a": 1}}
    r = dylan.client.post("/v1/tv/phone/report", headers=dylan.h,
                          json={"kind": "movie", "id": "vod:thomas-creek:101", "note": "stutters", "stats": stats})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "kept": 6}
    logged = json.dumps([dict(row) for row in hub.db.execute("SELECT * FROM activity").fetchall()])
    assert "tv.phone.report" in logged and "bitrate_kbps" in logged and "41.235" in logged
    assert SERVER not in logged and PASSWORD not in logged and "household" not in logged
    no = theo.client.post("/v1/tv/phone/report", headers=theo.h, json={"stats": {"route": "relay"}})
    assert no.status_code == 403 and no.json()["error"] == "forbidden"


def _two_episodes(hub, series="series:thomas-creek:50"):
    hub.tv_catalog.store_series({
        "info": {"name": "Cartoon Show"},
        "episodes": {"0": [{"id": "899", "episode_num": 1, "title": "Special"}],
                     "1": [{"id": "900", "episode_num": 1, "title": "Pilot"},
                           {"id": "901", "episode_num": 2, "title": "Second"}],
                     "2": [{"id": "910", "episode_num": 1, "title": "Back again"}]},
    }, series)


def test_an_episode_answer_names_the_next_episode(hub, people):
    _library(hub)
    _login(hub)
    _two_episodes(hub)
    dylan = people["phones"]["Dylan"]

    def play(eid, **extra):
        r = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"episode_id": eid, "player": "exo", **extra})
        assert r.status_code == 200, r.text
        return r.json()

    first = play("ep:thomas-creek:900")
    assert (first["season"], first["number"], first["series_name"]) == (1, 1, "Cartoon Show")
    assert first["next"]["episode_id"] == "ep:thomas-creek:901" and first["next"]["title"] == "Second"
    assert (first["next"]["season"], first["next"]["number"]) == (1, 2)
    assert first["next"]["series_id"] == "series:thomas-creek:50"
    # The last of a season leads into the next season; the last of the series has no next; a special is never next.
    assert play("ep:thomas-creek:901", direct=True)["next"]["episode_id"] == "ep:thomas-creek:910"
    assert "next" not in play("ep:thomas-creek:910")
    assert SERVER not in json.dumps(first["next"]) and PASSWORD not in json.dumps(first["next"])
    # A film has none of it.
    assert not {"next", "season", "number"} & set(_play(dylan).json())


def test_a_child_is_only_offered_a_kids_next_episode(hub, people):
    from hub.playernext import next_episode

    _library(hub)
    _login(hub)
    cat = hub.tv_catalog
    _two_episodes(hub)                               # "Cartoon Show" is in Kids
    assert next_episode(cat, "ep:thomas-creek:900", kids_only=True)["episode_id"] == "ep:thomas-creek:901"
    _two_episodes(hub, "series:thomas-creek:51")     # the same ids stored under "Drama Show"
    assert next_episode(cat, "ep:thomas-creek:900", kids_only=False)["episode_id"] == "ep:thomas-creek:901"
    assert next_episode(cat, "ep:thomas-creek:900", kids_only=True) is None
    assert next_episode(cat, "ep:thomas-creek:nope") is None
