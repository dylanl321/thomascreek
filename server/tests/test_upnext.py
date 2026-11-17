"""Up Next is ranked from what a profile has actually watched."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hub.upnext import remember, shelf

from tests.test_tv_api import _xt


def _catalog(hub):
    hub.tv_app.set_kids_categories(["Kids"])
    now = datetime.now(timezone.utc)
    xml = f"""<?xml version="1.0"?><tv>
      <programme start="{_xt(now - timedelta(minutes=2))}" stop="{_xt(now + timedelta(minutes=40))}" channel="cnn.us">
        <title>Newsroom</title>
      </programme>
      <programme start="{_xt(now - timedelta(minutes=2))}" stop="{_xt(now + timedelta(minutes=20))}" channel="toon.us">
        <title>Blue</title>
      </programme>
    </tv>""".encode()
    hub.tv_catalog.ingest(
        live=[
            {"stream_id": 10, "name": "CNN", "category_id": "1", "epg_channel_id": "cnn.us"},
            {"stream_id": 11, "name": "Cartoon", "category_id": "2", "epg_channel_id": "toon.us"},
            {"stream_id": 12, "name": "Sports", "category_id": "1"},
        ],
        live_categories=[{"category_id": "1", "category_name": "News"}, {"category_id": "2", "category_name": "Kids"}],
        vod=[{"stream_id": 101, "name": "Kids Movie", "category_id": "4"},
             {"stream_id": 100, "name": "Drama Movie", "category_id": "3"}],
        vod_categories=[{"category_id": "3", "category_name": "Drama"}, {"category_id": "4", "category_name": "Kids"}],
        series=[{"series_id": 50, "name": "Cartoon Show", "category_id": "5"}],
        series_categories=[{"category_id": "5", "category_name": "Kids"}],
        xmltv=xml,
    )
    hub.tv_catalog.store_series({
        "info": {"name": "Cartoon Show"},
        "episodes": {"1": [
            {"id": "900", "episode_num": 1, "title": "Pilot", "info": {"duration_secs": 1200}},
            {"id": "901", "episode_num": 2, "title": "Next", "info": {"duration_secs": 1200}},
        ]},
    }, "series:thomas-creek:50")


def test_shelf_ranks_history_and_hides_what_a_child_cannot_watch(hub, people):
    _catalog(hub)
    assert shelf(hub, "adult") == {"rows": []}
    now = datetime.now(timezone.utc).timestamp()
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}, now=now - 4000)
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}, now=now)
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "position_s": 600}, now=now)
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 50}, now=now)
    remember(hub.db, "adult", {
        "series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:900",
        "title": "Pilot", "position_s": 80,
    }, now=now)
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:12", "title": "Sports"}, now=now)
    remember(hub.db, "kids", {"channel_key": "xc:thomas-creek:11", "title": "Blue"}, now=now)
    hub.db.kv_set("dvr.recordings", [{
        "id": "dvr_abc123", "title": "Newsroom", "status": "done", "kids": False,
        "channel_key": "xc:thomas-creek:10", "poster_url": "/v1/art/poster/prog:abc",
        "capture_end": now,
    }, {
        "id": "dvr_kids1", "title": "Blue", "status": "done", "kids": True,
        "channel_key": "xc:thomas-creek:11", "capture_end": now - 10,
    }, {
        "id": "dvr_later", "title": "Later", "status": "scheduled", "kids": False, "capture_end": now,
    }])

    adult = shelf(hub, "adult")
    by_id = {row["id"]: row for row in adult["rows"]}
    assert list(by_id) == ["continue", "next", "recordings", "onnow", "trending"]
    continued = {item.get("movie_id") or item.get("episode_id") for item in by_id["continue"]["items"]}
    assert "vod:thomas-creek:101" in continued and "ep:thomas-creek:900" in continued
    assert by_id["continue"]["items"][0]["reason"] == "continue watching"
    assert by_id["next"]["items"][0]["episode_id"] == "ep:thomas-creek:901"
    assert by_id["next"]["items"][0]["reason"] == "new episode of a series you watch or record"
    assert [item["recording_id"] for item in by_id["recordings"]["items"]] == ["dvr_abc123", "dvr_kids1"]
    assert by_id["onnow"]["items"][0]["title"] == "Newsroom"
    assert by_id["onnow"]["items"][0]["channel_key"] == "xc:thomas-creek:10"
    assert by_id["onnow"]["items"][0]["reason"] == "starting now on a channel you watch"
    assert [item["channel_key"] for item in by_id["trending"]["items"]] == ["xc:thomas-creek:12"]
    assert by_id["trending"]["items"][0]["reason"] == "trending in your history"
    assert "://" not in str(adult)

    kids = shelf(hub, "kids")
    blob = str(kids)
    assert "Drama" not in blob and "Newsroom" not in blob and "dvr_abc123" not in blob
    assert any(item.get("channel_key") == "xc:thomas-creek:11" for row in kids["rows"] for item in row["items"])

    dylan = people["phones"]["Dylan"]
    theo = people["phones"]["Theo"]
    grown = dylan.client.get("/v1/tv/upnext", headers=dylan.h)
    assert grown.status_code == 200, grown.text
    assert grown.json()["rows"][0]["id"] == "continue"
    child = theo.client.get("/v1/tv/upnext", headers=theo.h, params={"profile": "adult"})
    assert child.status_code == 200, child.text
    assert "Drama" not in child.text and "Newsroom" not in child.text
    explicit = dylan.client.get("/v1/tv/upnext", headers=dylan.h, params={"profile": "kids"})
    assert "dvr_abc123" not in explicit.text


def test_continue_follows_the_person_and_falls_back_to_the_tv(hub, people):
    from hub.upnext import who_key

    _catalog(hub)
    now = datetime.now(timezone.utc).timestamp()
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 900},
             now=now, person="p_dylan", tv="tv-basement")
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "position_s": 600},
             now=now, tv="tv-office")

    def titles(who):
        rows = shelf(hub, "adult", who=who)["rows"]
        cont = next((r for r in rows if r["id"] == "continue"), {"items": []})
        return [i["title"] for i in cont["items"]]

    assert titles(who_key(person="p_dylan")) == ["Drama Movie"]
    assert titles(who_key(tv="tv-basement")) == ["Drama Movie"]
    assert titles(who_key(tv="tv-office")) == ["Kids Movie"]
    # Nobody with history of their own yet: the house's adult rows.
    assert sorted(titles(who_key(person="p_new"))) == ["Drama Movie", "Kids Movie"]


def test_the_players_length_draws_how_far_a_film_got(hub, people):
    """The catalog rarely knows a film's length; the TV sends it with the position, so the card gets a progress line."""
    _catalog(hub)
    now = datetime.now(timezone.utc).timestamp()
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 1800,
                               "duration_s": 7200}, now=now, tv="tv-office")
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "position_s": 600,
                               "duration_s": True}, now=now - 10, tv="tv-office")
    from hub.upnext import who_key

    rows = shelf(hub, "adult", who=who_key(tv="tv-office"))["rows"]
    cont = {i["title"]: i for i in next(r for r in rows if r["id"] == "continue")["items"]}
    assert cont["Drama Movie"]["progress"] == 0.25
    assert cont["Kids Movie"].get("progress") is None


def test_provider_names_are_cleaned_and_length_is_kept(hub, people, home_client):
    from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token

    hub.tv_catalog.ingest(vod=[{"stream_id": 300, "name": "EN - Resident Evil (2026)", "category_id": "1",
                                "container_extension": "mkv"}],
                          vod_categories=[{"category_id": "1", "category_name": "Action"}])
    key, clock = Key(), Clock()
    _enroll(home_client, key, TV_ID, "Office")
    hub.tv_app.set_tv(TV_ID, status="approved", room="office")
    auth = _auth(_token(home_client, key, clock)["access_token"])
    sent = home_client.post("/v1/tv/state", headers=auth, json={"playing": {
        "movie_id": "vod:thomas-creek:300", "title": "EN - Resident Evil (2026)", "position_s": 600, "duration_s": 6000}})
    assert sent.status_code == 200, sent.text
    rows = home_client.get("/v1/tv/upnext", headers=auth).json()["rows"]
    item = next(i for r in rows if r["id"] == "continue" for i in r["items"])
    assert item["title"] == "Resident Evil"
    assert item["progress"] == 0.1


def test_plex_films_continue_too(hub, people):
    from hub.upnext import who_key

    hub.db.kv_set("tv.plex.index", [{"kind": "movie", "id": "777", "title": "The Godfather", "year": 1972,
                                     "tmdb": 238, "rating": "R"}])
    remember(hub.db, "adult", {"movie_id": "vod:plex:777", "title": "The Godfather", "position_s": 1200,
                               "duration_s": 10500}, tv="tv-office")
    rows = shelf(hub, "adult", who=who_key(tv="tv-office"))["rows"]
    item = next(i for r in rows if r["id"] == "continue" for i in r["items"])
    assert item["movie_id"] == "vod:plex:777" and item["title"] == "The Godfather"
    assert item["poster_url"] == "/v1/tv/explore/art/poster/movie/238"
    assert shelf(hub, "kids", who=who_key(tv="tv-office"))["rows"] == [] or all(
        i.get("movie_id") != "vod:plex:777" for r in shelf(hub, "kids", who=who_key(tv="tv-office"))["rows"] for i in r["items"])


def test_a_shuffle_continues_on_any_screen_and_ends_when_played_in_order(hub, people):
    _catalog(hub)
    hub.tv_catalog.store_series({
        "info": {"name": "Cartoon Show"},
        "episodes": {"1": [
            {"id": "900", "episode_num": 1, "title": "Pilot"},
            {"id": "901", "episode_num": 2, "title": "Next"},
            {"id": "902", "episode_num": 3, "title": "Third"},
        ]},
    }, "series:thomas-creek:50")
    now = datetime.now(timezone.utc).timestamp()
    order = ["ep:thomas-creek:902", "ep:thomas-creek:900", "ep:thomas-creek:901"]
    remember(hub.db, "adult", {
        "series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:902", "title": "Third",
        "shuffle": {"order": order, "seasons": [1]},
    }, now=now, person="p_dylan")

    def nxt(who="person:p_dylan"):
        rows = {row["id"]: row for row in shelf(hub, "adult", who=who)["rows"]}
        return (rows.get("next") or {"items": [{}]})["items"][0]

    item = nxt()
    # In order the next would be nothing after 902; the shuffle says 900.
    assert item["episode_id"] == "ep:thomas-creek:900"
    assert item["reason"] == "next in your shuffle"
    assert item["shuffle"] == {"order": order, "seasons": [1]}

    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:901",
                               "title": "Next", "shuffle": {"order": order}}, now=now + 4000, person="p_dylan")
    assert nxt() == {}  # the last episode of the shuffle: nothing is next

    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:900",
                               "title": "Pilot"}, now=now + 8000, person="p_dylan")
    item = nxt()
    assert item["episode_id"] == "ep:thomas-creek:901" and "shuffle" not in item


def test_the_tv_state_keeps_a_clean_shuffle(hub):
    from hub.tvapp import _play_shuffle

    assert _play_shuffle({"order": ["ep:a", "ep:a", "bad id", "https://x/y", "ep:b"], "seasons": [2, 1, True, -3]}) \
        == {"order": ["ep:a", "ep:b"], "seasons": [1, 2]}
    assert _play_shuffle({"order": []}) is None
    assert _play_shuffle("yes") is None


def test_plex_episodes_continue_and_the_next_one_follows(hub, people, home_client):
    """A TV reports a Plex episode by its Plex id (#207): Apple TV under the show's key, Android under any key."""
    import httpx
    import respx

    from hub.upnext import KV_PLEX, who_key

    plex = "http://plex.test"
    hub.config.plex.enabled = True
    hub.config.plex.url = plex
    hub.vault.set("plex.token", "tok")

    def leaf(key, season, index, title):
        return {"ratingKey": key, "type": "episode", "title": title, "grandparentTitle": "Bluey",
                "grandparentRatingKey": "500", "parentIndex": season, "index": index, "duration": 420_000,
                "contentRating": "TV-Y"}

    show = {"MediaContainer": {"Metadata": [{"ratingKey": "500", "type": "show", "title": "Bluey",
                                             "contentRating": "TV-Y"}]}}
    leaves = {"MediaContainer": {"Metadata": [leaf("502", 1, 2, "Hospital"), leaf("501", 1, 1, "Magic Xylophone"),
                                              leaf("503", 2, 1, "Dance Mode")]}}
    dylan = people["phones"]["Dylan"]
    now = datetime.now(timezone.utc).timestamp()
    # Apple TV: the show's key and the episode's Plex id.
    remember(hub.db, "adult", {"series_id": "series:plex:500", "episode_id": "502", "title": "Hospital",
                               "position_s": 200, "duration_s": 420}, now=now, person=people["dylan"]["id"])
    with respx.mock(assert_all_called=False) as mock:
        meta = mock.get(f"{plex}/library/metadata/500").respond(json=show)
        mock.get(f"{plex}/library/metadata/500/allLeaves").respond(json=leaves)
        mock.get(f"{plex}/library/metadata/502").respond(json={"MediaContainer": {"Metadata": [leaf("502", 1, 2, "Hospital")]}})
        body = dylan.client.get("/v1/tv/upnext", headers=dylan.h).json()
        rows = {r["id"]: r["items"] for r in body["rows"]}
        [cont] = rows["continue"]
        assert cont["episode_id"] == "502" and cont["series_id"] == "series:plex:500"
        assert cont["title"] == "S1E2 · Hospital" and cont["subtitle"] == "Bluey"
        assert cont["position_s"] == 200 and 0.4 < cont["progress"] < 0.5
        [nxt] = rows["next"]
        assert nxt["episode_id"] == "503" and nxt["title"] == "S2E1 · Dance Mode"  # season order, not Plex's
        calls = meta.call_count
        dylan.client.get("/v1/tv/upnext", headers=dylan.h)
        assert meta.call_count == calls  # kept for hours, not asked again per screen

        # Android: a folded series key, so the hub asks Plex which show the episode belongs to.
        hub.db.kv_set(KV_PLEX, None)
        remember(hub.db, "adult", {"series_id": "series:thomas-creek:77", "episode_id": "502", "title": "Hospital",
                                   "position_s": 90}, now=now + 1, tv="tv-den")
        from hub.upnext import shelf as build, warm_plex
        import asyncio
        asyncio.run(warm_plex(hub, who_key(tv="tv-den"), "adult"))
        rows = {r["id"]: r["items"] for r in build(hub, "adult", who=who_key(tv="tv-den"))["rows"]}
        assert rows["continue"][0]["episode_id"] == "502" and rows["continue"][0]["subtitle"] == "Bluey"

    # Plex not answering: the episode is left out, nothing else breaks.
    hub.db.kv_set(KV_PLEX, None)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=plex).mock(return_value=httpx.Response(500))
        body = dylan.client.get("/v1/tv/upnext", headers=dylan.h)
        assert body.status_code == 200
        assert all(i.get("episode_id") != "502" for r in body.json()["rows"] for i in r["items"])


def test_old_history_moves_into_its_own_table_once(tmp_path):
    """The one-document history (hub_kv "tv.watch") is carried into watch_history when the hub starts."""
    from hub.db import DB

    path = tmp_path / "hub.db"
    db = DB(path)
    db.kv_set("tv.watch", {
        "adult": {"channels": {"xc:a:1": {"n": 3, "last": 100.0, "title": "News"}}, "series": {}, "movies": {}},
        "person:p1": {"movies": {"vod:a:9": {"n": 1, "last": 200.0, "position_s": 600}}},
    })
    db = DB(path)
    assert db.kv_get("tv.watch") is None
    assert db.watch_bucket("adult")["channels"]["xc:a:1"]["n"] == 3
    assert db.watch_bucket("person:p1") == {"movies": {"vod:a:9": {"n": 1, "last": 200.0, "position_s": 600}}}
    remember(db, "adult", {"movie_id": "vod:a:9", "title": "Film", "position_s": 700}, now=300.0, person="p1")
    again = DB(path)
    assert again.watch_bucket("person:p1")["movies"]["vod:a:9"]["position_s"] == 700
    assert again.watch_bucket("adult")["movies"]["vod:a:9"]["position_s"] == 700
