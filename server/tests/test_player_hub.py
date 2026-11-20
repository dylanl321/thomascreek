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


def test_a_busy_answer_names_the_hub_when_its_own_job_holds_the_login(hub, people):
    from hub.iptvplay import direct

    _library(hub)
    _login(hub)
    assert direct(hub, FILM["movie_id"], "hub:thumbs")
    busy = _play(people["phones"]["Avery"], direct=True)
    assert busy.status_code == 503, busy.text
    assert busy.json()["holders"][0]["kind"] == "hub"
    assert busy.json()["holders"][0]["name"] == "The hub (making previews)"


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


def _with_tmdb(hub, monkeypatch, tmdb="1396"):
    real = hub.tv_catalog.known_row

    def known(kind, key):
        row = real(kind, key)
        return {**row, "tmdb": tmdb} if row and kind == "series" else row
    monkeypatch.setattr(hub.tv_catalog, "known_row", known)


def test_segments_come_from_the_hub_cleaned_and_kept(hub, people, monkeypatch):
    import httpx
    import respx

    from hub.playersegments import API

    _library(hub)
    _two_episodes(hub)
    _with_tmdb(hub, monkeypatch)
    dylan = people["phones"]["Dylan"]
    url = "/v1/tv/phone/segments?episode_id=ep:thomas-creek:901&duration_s=1500"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(API).mock(return_value=httpx.Response(200, json={
            "recap": [{"start_ms": 0, "end_ms": 45000}],
            "intro": [{"start_ms": 45000, "end_ms": 105000}, {"start_ms": 1200000, "end_ms": 1260000},  # mid-episode
                      {"start_ms": 50000, "end_ms": None}],                                          # no end
            "credits": [{"start_ms": 1440000, "end_ms": None}, {"start_ms": 300000, "end_ms": 400000}],
        }))
        got = dylan.client.get(url, headers=dylan.h)
        assert got.status_code == 200, got.text
        assert got.json()["segments"] == [
            {"kind": "recap", "start_s": 0.0, "end_s": 45.0},
            {"kind": "intro", "start_s": 45.0, "end_s": 105.0},
            {"kind": "credits", "start_s": 1440.0, "end_s": 1500.0},
        ]
        asked = dict(route.calls.last.request.url.params)
        assert asked == {"tmdb_id": "1396", "season": "1", "episode": "2", "duration_ms": "1500000"}
        # Asked once for the house: the second player gets the kept answer.
        assert dylan.client.get(url, headers=dylan.h).json() == got.json() and route.call_count == 1


def test_segments_are_empty_without_an_answer_and_never_for_a_child_on_an_adult_title(hub, people, monkeypatch):
    import httpx
    import respx

    from hub.playersegments import API

    _library(hub)
    _two_episodes(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    url = "/v1/tv/phone/segments?episode_id=ep:thomas-creek:900"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(API).mock(side_effect=httpx.ConnectError("down"))
        # No TMDB id for the series: nothing is asked.
        assert dylan.client.get(url, headers=dylan.h).json() == {"segments": []} and not route.called
        _with_tmdb(hub, monkeypatch)
        # The service is down: an empty list, not an error.
        assert dylan.client.get(url, headers=dylan.h).json() == {"segments": []} and route.called
    assert dylan.client.get("/v1/tv/phone/segments", headers=dylan.h).status_code == 400
    assert dylan.client.get("/v1/tv/phone/segments?episode_id=ep:thomas-creek:nope", headers=dylan.h).status_code == 404
    _two_episodes(hub, "series:thomas-creek:51")   # now a "Drama Show" episode
    assert theo.client.get(url, headers=theo.h).status_code == 404


def test_play_next_queue_follows_the_person_and_keeps_to_kids_titles_for_a_child(hub, people):
    _library(hub)
    _two_episodes(hub)
    dylan, avery, theo = (people["phones"][n] for n in ("Dylan", "Avery", "Theo"))

    def change(phone, **body):
        r = phone.client.post("/v1/tv/phone/queue", headers=phone.h, json=body)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    assert dylan.client.get("/v1/tv/phone/queue", headers=dylan.h).json() == {"items": []}
    assert change(dylan, add="vod:thomas-creek:100") == ["vod:thomas-creek:100"]
    assert change(dylan, add="ep:thomas-creek:901") == ["vod:thomas-creek:100", "ep:thomas-creek:901"]
    assert change(dylan, add="vod:thomas-creek:101", first=True)[0] == "vod:thomas-creek:101"
    # Adding again moves it; order only reorders what is there.
    again = change(dylan, add="vod:thomas-creek:100")
    assert again == ["vod:thomas-creek:101", "ep:thomas-creek:901", "vod:thomas-creek:100"]
    assert change(dylan, order=["vod:thomas-creek:100", "vod:thomas-creek:999"])[0] == "vod:thomas-creek:100"
    items = dylan.client.get("/v1/tv/phone/queue", headers=dylan.h).json()["items"]
    episode = next(i for i in items if i["kind"] == "episode")
    assert (episode["title"], episode["subtitle"]) == ("Second", "Cartoon Show")
    assert (episode["season"], episode["number"]) == (1, 2)
    assert next(i for i in items if i["id"] == "vod:thomas-creek:101")["title"] == "Kids Movie"
    assert SERVER not in json.dumps(items) and PASSWORD not in json.dumps(items)
    # Each person has their own.
    assert avery.client.get("/v1/tv/phone/queue", headers=avery.h).json() == {"items": []}
    assert change(dylan, remove="ep:thomas-creek:901") == ["vod:thomas-creek:100", "vod:thomas-creek:101"]
    # Something not in the library can't be queued.
    bad = dylan.client.post("/v1/tv/phone/queue", headers=dylan.h, json={"add": "vod:thomas-creek:999"})
    assert bad.status_code == 404
    # A child can queue a kids' film, never an adult one.
    assert change(theo, add="vod:thomas-creek:101") == ["vod:thomas-creek:101"]
    no = theo.client.post("/v1/tv/phone/queue", headers=theo.h, json={"add": "vod:thomas-creek:100"})
    assert no.status_code == 404
    # An adult title that got into a child's queue another way is never shown to the child.
    from hub import playerqueue
    owner = next(k for k, v in hub.db.kv_get(playerqueue.KV).items() if v == ["vod:thomas-creek:101"])
    playerqueue.change(hub, owner, add="vod:thomas-creek:100")
    seen = theo.client.get("/v1/tv/phone/queue", headers=theo.h).json()["items"]
    assert [i["id"] for i in seen] == ["vod:thomas-creek:101"]
    assert change(dylan, clear=True) == []


OS_SEARCH = {"data": [
    {"attributes": {"language": "en", "release": "Cartoon.Show.S01E02.WEB", "hearing_impaired": True,
                    "download_count": 900, "files": [{"file_id": 111, "file_name": "a.srt"}]}},
    {"attributes": {"language": "en", "release": "Cartoon.Show.S01E02.BluRay", "download_count": 400,
                    "files": [{"file_id": 222, "file_name": "b.srt"}]}},
    {"attributes": {"language": "en", "release": "machine", "ai_translated": True, "download_count": 9999,
                    "files": [{"file_id": 333}]}},
    {"attributes": {"language": "en", "release": "two discs", "files": [{"file_id": 444}, {"file_id": 445}]}},
]}
SRT = "1\r\n00:00:01,000 --> 00:00:02,500\r\nHello there\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nCaf\xe9\r\n"


def test_subtitles_are_searched_and_fetched_by_the_hub(hub, people, monkeypatch):
    import httpx
    import respx

    from hub import playersubs

    _library(hub)
    _two_episodes(hub)
    _with_tmdb(hub, monkeypatch)
    dylan = people["phones"]["Dylan"]
    url = "/v1/tv/phone/subtitles?episode_id=ep:thomas-creek:901"
    # Without a key: an empty list and why, and nothing is asked.
    none = dylan.client.get(url, headers=dylan.h).json()
    assert none["subtitles"] == [] and "key" in none["reason"]
    hub.vault.set(playersubs.KEY, "os-test-key")
    with respx.mock(assert_all_called=False) as mock:
        search = mock.get(f"{playersubs.API}/subtitles").mock(return_value=httpx.Response(200, json=OS_SEARCH))
        ticket = mock.post(f"{playersubs.API}/download").mock(
            return_value=httpx.Response(200, json={"link": "https://dl.example/b.srt", "file_name": "b.srt"}))
        mock.get("https://dl.example/b.srt").mock(return_value=httpx.Response(200, content=SRT.encode("cp1252")))
        got = dylan.client.get(url, headers=dylan.h)
        assert got.status_code == 200, got.text
        subs = got.json()["subtitles"]
        # Made by people, one file each; the plain one before the one for the hard of hearing.
        assert [s["id"] for s in subs] == ["222", "111"] and subs[0]["label"] == "Cartoon.Show.S01E02.BluRay"
        asked = dict(search.calls.last.request.url.params)
        assert asked == {"languages": "en", "parent_tmdb_id": "1396", "season_number": "1", "episode_number": "2"}
        assert search.calls.last.request.headers["Api-Key"] == "os-test-key"
        assert "os-test-key" not in got.text
        # Kept for the house: the second ask is not a second search.
        assert dylan.client.get(url, headers=dylan.h).json()["subtitles"] == subs and search.call_count == 1

        file = dylan.client.get(subs[0]["url"], headers=dylan.h)
        assert file.status_code == 200 and file.headers["content-type"].startswith("text/vtt")
        assert file.text.startswith("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nHello there") and "Café" in file.text
        # Downloaded once (the service counts them), then served from the hub.
        assert dylan.client.get(subs[0]["url"], headers=dylan.h).text == file.text and ticket.call_count == 1
        # Only a file the hub offered for this title is ever downloaded.
        other = "/v1/tv/phone/subtitles/999.vtt?episode_id=ep:thomas-creek:901"
        assert dylan.client.get(other, headers=dylan.h).status_code == 404 and ticket.call_count == 1
        wrong = "/v1/tv/phone/subtitles/222.vtt?episode_id=ep:thomas-creek:900"
        assert dylan.client.get(wrong, headers=dylan.h).status_code == 404


def test_subtitles_say_why_when_the_service_fails_and_keep_the_kids_rule(hub, people, monkeypatch):
    import httpx
    import respx

    from hub import playersubs

    _library(hub)
    _two_episodes(hub, "series:thomas-creek:51")     # a "Drama Show" episode
    hub.vault.set(playersubs.KEY, "os-test-key")
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    url = "/v1/tv/phone/subtitles?episode_id=ep:thomas-creek:901&lang=de"
    with respx.mock(assert_all_called=False) as mock:
        search = mock.get(f"{playersubs.API}/subtitles").mock(side_effect=httpx.ConnectError("down"))
        got = dylan.client.get(url, headers=dylan.h)
        assert got.status_code == 200
        assert got.json() == {"subtitles": [], "reason": "The subtitle service didn't answer"}
        # No TMDB id: searched by the series' clean name.
        asked = dict(search.calls.last.request.url.params)
        assert asked["query"] == "Drama Show" and asked["languages"] == "de" and "parent_tmdb_id" not in asked
    assert theo.client.get(url, headers=theo.h).status_code == 404
    file = "/v1/tv/phone/subtitles/222.vtt?episode_id=ep:thomas-creek:901"
    assert theo.client.get(file, headers=theo.h).status_code == 404
    assert dylan.client.get("/v1/tv/phone/subtitles", headers=dylan.h).status_code == 400
    assert playersubs.to_vtt(b"not a subtitle") is None
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\nHi"
    assert playersubs.to_vtt(vtt.encode()) == vtt + "\n"


NIGHT = 1795158000.0      # 02:00 on a November night in the house's time zone
DAY = 1795201200.0        # 14:00 the same day


def _watched(phone, **item):
    done = phone.client.post("/v1/tv/phone/progress", headers=phone.h, json={**item, "position_s": 600})
    assert done.status_code == 200, done.text


def _fake_ffmpeg(monkeypatch, during=None, sheets=2):
    from pathlib import Path

    from hub import iptvplay, playerthumbs

    seen = {"runs": 0, "args": []}

    async def run(args, keep_going):
        seen["runs"] += 1
        seen["args"] = args
        seen["held"] = [row["tv"] for row in iptvplay.accounts(seen["hub"])["sessions"]]
        if during is not None:
            during()
            if not await keep_going():
                return False
        for n in range(1, sheets + 1):
            (Path(args[-1]).parent / f"{n:02d}.jpg").write_bytes(b"\xff\xd8 sheet")
        return True

    monkeypatch.setattr(playerthumbs, "_run", run)
    monkeypatch.setattr(playerthumbs.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    return seen


def test_scrub_pictures_are_made_at_night_on_a_spare_login_and_follow_the_kids_rule(
        hub, people, monkeypatch, tmp_path):
    import asyncio

    from hub import iptvplay, playerthumbs

    _library(hub)
    _login(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    _watched(dylan, movie_id="vod:thomas-creek:100")
    assert playerthumbs.wanted(hub) == ["vod:thomas-creek:100"]
    assert playerthumbs.at_night(hub, NIGHT) and not playerthumbs.at_night(hub, DAY)
    url = "/v1/tv/phone/thumbs?movie_id=vod:thomas-creek:100"
    empty = dylan.client.get(url, headers=dylan.h).json()
    assert empty["sheets"] == [] and (empty["interval_s"], empty["columns"], empty["rows"]) == (10, 10, 10)

    seen = _fake_ffmpeg(monkeypatch)
    seen["hub"] = hub
    folder = tmp_path / "thumbs"
    # By day nothing runs. At night with one login nothing runs either: a person could be left waiting.
    assert asyncio.run(playerthumbs.night(hub, folder, now=DAY)) == 0
    assert asyncio.run(playerthumbs.night(hub, folder, now=NIGHT)) == 0 and seen["runs"] == 0
    assert iptvplay.accounts(hub)["sessions"] == []

    iptvplay.add_login(hub, "thomas-creek", SERVER, "two", "second", "second-pass")
    monkeypatch.setattr(hub.tv_app, "dir", tmp_path)
    assert asyncio.run(playerthumbs.night(hub, folder, now=NIGHT)) == 1
    assert seen["held"] == [playerthumbs.HOLDER]              # the job read it on a leased login
    assert iptvplay.accounts(hub)["sessions"] == []           # and gave the login back
    assert playerthumbs.wanted(hub) == []
    # The address went to ffmpeg alone: not into the log, the kept note or any answer.
    kept = json.dumps(hub.db.kv_get(playerthumbs.KV)) + json.dumps(
        [dict(r) for r in hub.db.all("SELECT * FROM activity WHERE action='player.thumbs'")], default=str)
    assert any(SERVER in arg for arg in seen["args"]) and SERVER not in kept and PASSWORD not in kept

    made = dylan.client.get(url, headers=dylan.h).json()
    assert len(made["sheets"]) == 2 and SERVER not in json.dumps(made)
    assert made["sheets"][0].endswith("/1.jpg?movie_id=vod%3Athomas-creek%3A100")
    one = dylan.client.get(made["sheets"][0], headers=dylan.h)
    assert one.status_code == 200 and one.headers["content-type"] == "image/jpeg"
    assert one.content.startswith(b"\xff\xd8")
    # A sheet is only handed out for its own title, and never to a child for a grown-up's film.
    path = made["sheets"][0].split("?")[0]
    other = dylan.client.get(path, headers=dylan.h, params={"movie_id": "vod:thomas-creek:101"})
    assert other.status_code == 404
    assert theo.client.get(url, headers=theo.h).status_code == 404
    kid = theo.client.get(made["sheets"][0], headers=theo.h)
    assert kid.status_code == 404


def test_the_night_job_gives_way_the_moment_someone_plays(hub, people, monkeypatch, tmp_path):
    import asyncio

    from hub import iptvplay, playerthumbs

    _library(hub)
    _login(hub)
    iptvplay.add_login(hub, "thomas-creek", SERVER, "two", "second", "second-pass")
    dylan = people["phones"]["Dylan"]
    _watched(dylan, movie_id="vod:thomas-creek:100")
    folder = tmp_path / "thumbs"

    # Someone is already playing: the job does not start.
    seen = _fake_ffmpeg(monkeypatch)
    seen["hub"] = hub
    assert _play(dylan, direct=True).status_code == 200
    assert asyncio.run(playerthumbs.make(hub, folder, "vod:thomas-creek:100")) == "wait" and seen["runs"] == 0
    iptvplay.release_tv(hub, f"phone:{dylan.device_id}")

    # Someone starts while it runs: it stops, frees its login, keeps nothing and tries again another night.
    seen = _fake_ffmpeg(monkeypatch, during=lambda: _play(dylan, direct=True))
    seen["hub"] = hub
    assert asyncio.run(playerthumbs.make(hub, folder, "vod:thomas-creek:100")) == "wait" and seen["runs"] == 1
    assert [row["tv"] for row in iptvplay.accounts(hub)["sessions"]] == [f"phone:{dylan.device_id}"]
    assert not list(folder.glob("*")) or not any(p.glob("*.jpg") for p in folder.iterdir())
    assert playerthumbs.wanted(hub) == ["vod:thomas-creek:100"]

    # ffmpeg failing is noted, so the same title is not tried every night.
    iptvplay.release_tv(hub, f"phone:{dylan.device_id}")
    seen = _fake_ffmpeg(monkeypatch, sheets=0)
    seen["hub"] = hub
    assert asyncio.run(playerthumbs.make(hub, folder, "vod:thomas-creek:100")) == "failed"
    assert playerthumbs.wanted(hub) == [] and iptvplay.accounts(hub)["sessions"] == []


def test_queue_and_next_episode_show_clean_names_not_the_providers():
    from hub import playerqueue
    from hub.playernext import episode_name

    assert episode_name("EN - Cartoon Show (2009) - S01E02 - Second", 2) == "Second"
    assert episode_name("Cartoon Show S01 E03", 3) == "Episode 3"
    assert episode_name("Second") == "Second"
    assert episode_name(None) == "Episode"

    class Cat:
        def episode(self, key):
            return {"title": "EN - Cartoon Show (2009) - S01E02 - Second", "series_id": "series:p:5", "season": 1,
                    "number": 2, "series_name": "EN - Cartoon Show (2009)"}

        def movie(self, key):
            return {"name": "NF - Kids Movie (2023)", "year": 2023}

        def _art(self, kind, key):
            return f"/v1/tv/art/{kind}/x" if kind == "poster" and key else None

    episode, film = playerqueue.present(Cat(), ["ep:p:9", "vod:p:1"], lambda row: True)
    assert (episode["title"], episode["subtitle"]) == ("Second", "Cartoon Show")
    assert episode["image_url"] == "/v1/tv/art/poster/x"      # no still: the series' poster
    assert film["title"] == "Kids Movie"


def test_a_tv_reads_the_next_episode_and_keeps_a_queue_like_a_phone(hub, home_client, people):
    from tests.test_tv_who import _tv

    _library(hub)
    _two_episodes(hub)
    tv = _tv(hub, home_client)
    got = home_client.get("/v1/tv/phone/next?episode_id=ep:thomas-creek:900", headers=tv)
    assert got.status_code == 200, got.text
    assert got.json()["next"]["episode_id"] == "ep:thomas-creek:901"
    assert home_client.get("/v1/tv/phone/next?episode_id=ep:thomas-creek:910", headers=tv).json() == {}
    assert home_client.get("/v1/tv/phone/next?episode_id=ep:thomas-creek:nope", headers=tv).status_code == 404
    # With nobody picked the TV has its own queue, apart from any person's.
    added = home_client.post("/v1/tv/phone/queue", headers=tv, json={"add": "vod:thomas-creek:100"})
    assert added.status_code == 200, added.text
    assert [i["id"] for i in added.json()["items"]] == ["vod:thomas-creek:100"]
    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/tv/phone/queue", headers=dylan.h).json() == {"items": []}
    # A phone reads the next episode the same way.
    assert dylan.client.get("/v1/tv/phone/next?episode_id=ep:thomas-creek:900",
                            headers=dylan.h).json()["next"]["title"] == "Second"


def test_a_kids_tv_is_only_offered_kids_titles_next_and_in_its_queue(hub, home_client, people):
    from tests.test_tv_who import _tv

    _library(hub)
    _two_episodes(hub)
    tv = _tv(hub, home_client, profile="kids")
    url = "/v1/tv/phone/next?episode_id=ep:thomas-creek:900"
    # "Cartoon Show" is in Kids: its next episode is offered. An adult film can't be queued from a kids' TV.
    assert home_client.get(url, headers=tv).json()["next"]["episode_id"] == "ep:thomas-creek:901"
    assert home_client.post("/v1/tv/phone/queue", headers=tv, json={"add": "vod:thomas-creek:100"}).status_code != 200
    assert home_client.post("/v1/tv/phone/queue", headers=tv, json={"add": "vod:thomas-creek:101"}).status_code == 200
    # The same ids under "Drama Show": the kids' TV is refused the episode, so it learns no "next".
    _two_episodes(hub, "series:thomas-creek:51")
    assert home_client.get(url, headers=tv).status_code == 404


def test_next_episode_sees_an_episode_added_after_the_first_lookup():
    from types import SimpleNamespace

    from hub.playernext import next_episode

    doc = {"episodes": {"ep:a:1": {"id": "ep:a:1", "series_id": "series:a:9", "season": 1, "number": 1}}}
    cat = SimpleNamespace(_details=lambda: doc, _details_loaded=((1, 1), doc), _art=lambda *_a: None,
                          episode=lambda _i: {"kids": True})
    assert next_episode(cat, "ep:a:1") is None
    doc["episodes"]["ep:a:2"] = {"id": "ep:a:2", "series_id": "series:a:9", "season": 1, "number": 2}
    cat._details_loaded = ((2, 2), doc)  # a write changes the file's stamp
    assert next_episode(cat, "ep:a:1")["episode_id"] == "ep:a:2"
