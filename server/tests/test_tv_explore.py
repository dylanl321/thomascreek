"""Explore only offers a title the house can play or ask for."""

from __future__ import annotations

from hub.routes.home import shape_event
import asyncio

from hub.tvexplore import advance, publish, remember_plex

LISTS = {
    "new": [{
        "tmdb_id": 603, "kind": "movie", "title": "The Matrix", "year": 1999,
        "overview": "A hacker.", "tagline": "Welcome to the Real World", "rating": "R",
        "genres": ["Action", "Science Fiction"],
        "poster_path": "/m.jpg", "backdrop_path": "/b.jpg", "logo_path": "/l.png",
    }],
    "popular": [{
        "tmdb_id": 82728, "kind": "series", "title": "Bluey", "year": 2018,
        "overview": "A puppy.", "rating": "TV-Y", "genres": ["Kids"],
        "poster_path": "/p.jpg", "backdrop_path": "/d.jpg",
    }],
    "acclaimed": [{
        "tmdb_id": 129, "kind": "movie", "title": "Spirited Away", "year": 2001,
        "overview": "A girl.", "rating": "PG", "genres": ["Animation"],
        "poster_path": "/s.jpg", "backdrop_path": "/sb.jpg",
    }],
}

ADULT = {
    "hero": {
        "kind": "movie", "title": "The Matrix", "year": 1999, "overview": "A hacker.",
        "tagline": "Welcome to the Real World",
        "poster_url": "/v1/tv/explore/art/poster/movie/603",
        "backdrop_url": "/v1/tv/explore/art/backdrop/movie/603",
        "logo_url": "/v1/tv/explore/art/logo/movie/603",
        "title_card_url": "/v1/tv/explore/art/title/movie/603",
        "rating": "R", "genres": ["Action", "Science Fiction"],
        "reason": "New this week", "badges": ["New"], "availability": "play",
        "movie_id": "vod:thomas-creek:100",
    },
    "rows": [
        {"id": "new", "title": "New releases", "items": [{
            "kind": "movie", "title": "The Matrix", "year": 1999, "overview": "A hacker.",
            "tagline": "Welcome to the Real World",
            "poster_url": "/v1/tv/explore/art/poster/movie/603",
            "backdrop_url": "/v1/tv/explore/art/backdrop/movie/603",
            "logo_url": "/v1/tv/explore/art/logo/movie/603",
            "title_card_url": "/v1/tv/explore/art/title/movie/603",
            "rating": "R", "genres": ["Action", "Science Fiction"],
            "reason": "New this week", "badges": ["New"], "availability": "play",
            "movie_id": "vod:thomas-creek:100",
        }]},
        {"id": "popular", "title": "Popular", "items": [{
            "kind": "series", "title": "Bluey", "year": 2018, "overview": "A puppy.", "tagline": None,
            "poster_url": "/v1/tv/explore/art/poster/tv/82728",
            "backdrop_url": "/v1/tv/explore/art/backdrop/tv/82728",
            "logo_url": "/v1/tv/explore/art/logo/tv/82728",
            "title_card_url": "/v1/tv/explore/art/title/tv/82728",
            "rating": "TV-Y", "genres": ["Kids"],
            "reason": "Popular this week", "badges": ["Popular", "Top 10"], "availability": "request",
            "tmdb_id": 82728, "media_id": "tv:82728",
        }]},
        {"id": "acclaimed", "title": "Must watch", "items": [{
            "kind": "movie", "title": "Spirited Away", "year": 2001, "overview": "A girl.", "tagline": None,
            "poster_url": "/v1/tv/explore/art/poster/movie/129",
            "backdrop_url": "/v1/tv/explore/art/backdrop/movie/129",
            "logo_url": "/v1/tv/explore/art/logo/movie/129",
            "title_card_url": "/v1/tv/explore/art/title/movie/129",
            "rating": "PG", "genres": ["Animation"],
            "reason": "Acclaimed", "badges": ["Top 10"], "availability": "play",
            "movie_id": "vod:thomas-creek:129",
        }]},
    ],
}


def _library(hub) -> None:
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 100, "name": "The Matrix", "year": "1999", "category_id": "1"},
        {"stream_id": 129, "name": "Spirited Away", "year": "2001", "category_id": "2"},
        {"stream_id": 101, "name": "Kids Movie", "year": "2018", "category_id": "4", "genre": "Kids", "rating": "G"},
    ], vod_categories=[
        {"category_id": "1", "category_name": "Action"},
        {"category_id": "2", "category_name": "Animation"},
        {"category_id": "4", "category_name": "Kids"},
    ])


def test_the_feed_is_the_contract(hub, people):
    _library(hub)
    publish(hub, LISTS, can_request=True)
    dylan = people["phones"]["Dylan"]
    theo = people["phones"]["Theo"]
    assert dylan.client.get("/v1/tv/explore", headers=dylan.h).json() == ADULT
    kids = theo.client.get("/v1/tv/explore?profile=adult", headers=theo.h).json()
    titles = [item["title"] for row in kids["rows"] for item in row["items"]]
    assert "The Matrix" not in titles and "Bluey" in titles and "Spirited Away" in titles
    assert kids["rows"][0]["items"][0]["availability"] in ("play", "request")
    assert dylan.client.get("/v1/tv/explore/art/poster/movie/603", headers=dylan.h).status_code == 404
    assert dylan.client.get("/v1/tv/explore/art/poster/movie/603", headers=dylan.h).json()["message"] == "No artwork"


def test_a_plex_shelf_remembers_the_thumb_and_the_art(hub):
    hub.plex.cfg.enabled = True
    hub.plex.cfg.url = "http://plex.test"

    async def recently_added(limit=20):
        return [{
            "id": "42", "title": "WALL-E", "year": 2008, "kind": "movie", "rating": "G",
            "thumb_path": "/library/metadata/42/thumb/1",
            "art_path": "/library/metadata/42/art/1",
            "poster_url": "/v1/media/art?path=%2Flibrary%2Fmetadata%2F42%2Fthumb%2F1",
        }]

    hub.plex.recently_added = recently_added
    asyncio.run(remember_plex(hub))
    note = hub.artwork.note("vod:plex:42")
    assert note["plex_thumb"] == "/library/metadata/42/thumb/1"
    assert note["plex_art"] == "/library/metadata/42/art/1"
    stored = hub.db.kv_get("tv.plex.recent")
    assert stored[0]["poster_url"] == "/v1/art/poster/vod:plex:42"
    assert stored[0]["backdrop_url"] == "/v1/art/backdrop/vod:plex:42"


def test_plex_and_a_recording_thumbnail_join_the_library(hub, people):
    hub.db.kv_set("dvr.recordings", [{
        "id": "clip1", "title": "Evening News", "status": "done",
        "thumb": "https://cdn.example/news.jpg",
    }])
    hub.db.kv_set("tv.plex.recent", [{
        "kind": "movie", "title": "WALL-E", "year": 2008, "movie_id": "vod:plex:42", "plex_id": "42",
        "poster_url": "/v1/art/poster/vod:plex:42", "backdrop_url": "/v1/art/backdrop/vod:plex:42",
    }])
    publish(hub, None, can_request=False)
    dylan = people["phones"]["Dylan"]
    titles = {
        item["title"]: item
        for row in dylan.client.get("/v1/tv/explore", headers=dylan.h).json()["rows"]
        for item in row["items"]
    }
    assert titles["Evening News"]["poster_url"] == "/v1/art/poster/vod:rec:clip1"
    assert titles["Evening News"]["backdrop_url"] == "/v1/art/backdrop/vod:rec:clip1"
    assert titles["WALL-E"]["poster_url"] == "/v1/art/poster/vod:plex:42"
    assert titles["WALL-E"]["backdrop_url"] == "/v1/art/backdrop/vod:plex:42"
    assert titles["WALL-E"]["availability"] == "play"


def test_no_tmdb_key_uses_the_library(hub, people):
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 101, "name": "Kids Movie", "year": "2018", "category_id": "4", "genre": "Kids", "rating": "G"},
    ], vod_categories=[{"category_id": "4", "category_name": "Kids"}])
    publish(hub, None, can_request=False)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    card = {
        "kind": "movie", "title": "Kids Movie", "year": 2018, "overview": None, "tagline": None,
        "poster_url": "/v1/art/poster/vod:thomas-creek:101",
        "backdrop_url": "/v1/art/backdrop/vod:thomas-creek:101",
        "logo_url": None, "title_card_url": None, "rating": "G", "genres": ["Kids"], "reason": "In your library", "badges": ["New"],
        "availability": "play", "movie_id": "vod:thomas-creek:101",
    }
    assert body == {"hero": card, "rows": [{"id": "library", "title": "Recently added", "items": [card]}]}
    hub.db.kv_set("tv.explore", {})
    hub.tv_catalog.ingest()
    assert dylan.client.get("/v1/tv/explore", headers=dylan.h).json() == {"hero": None, "rows": []}


def test_one_tap_asks_and_a_ready_title_reaches_up_next(hub, people):
    _library(hub)
    publish(hub, LISTS, can_request=True)
    dylan = people["phones"]["Dylan"]
    theo = people["phones"]["Theo"]
    hub.config.jellyseerr.enabled = True
    hub.config.jellyseerr.url = "http://js.test"
    hub.config.hub.live_actions = ["media.approve"]

    async def submit(kind, tmdb, seasons):
        return {"id": 88}

    hub.jellyseerr.submit = submit  # type: ignore[method-assign]
    already = dylan.client.post("/v1/tv/explore/request", headers=dylan.h, json={"tmdb_id": 603, "kind": "movie"})
    assert already.json() == {"ok": True, "status": "play", "movie_id": "vod:thomas-creek:100",
                              "message": "It's already in the library."}
    asked = dylan.client.post("/v1/tv/explore/request", headers=dylan.h, json={"tmdb_id": 82728, "kind": "series"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "requested" and asked.json()["message"] == "Asked for Bluey."
    assert asked.json()["request_id"] == "88"
    grown = theo.client.post("/v1/tv/explore/request", headers=theo.h, json={"tmdb_id": 603, "kind": "movie"})
    assert grown.json() == {"ok": False, "status": "unavailable", "message": "That one needs a grown-up."}
    waiting = theo.client.post("/v1/tv/explore/request", headers=theo.h, json={"tmdb_id": 82728, "kind": "series"})
    assert waiting.json()["status"] == "waiting" and waiting.json()["message"] == "Asked a parent."
    assert dylan.client.post("/v1/tv/explore/request", headers=dylan.h, json={"kind": "movie"}).json()["message"] == "Say which title."

    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    phone = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id, subject.device_id)
    advance(hub, 82728, "series", "ready", series_id="series:thomas-creek:50")
    event = phone.queue.get_nowait()
    assert event["type"] == "explore" and event["data"]["status"] == "ready" and event["data"]["series_id"] == "series:thomas-creek:50"
    shelf = dylan.client.get("/v1/tv/upnext", headers=dylan.h).json()
    ready = next(row for row in shelf["rows"] if row["id"] == "recordings")
    assert ready["items"][0]["reason"] == "ready to watch" and ready["items"][0]["series_id"] == "series:thomas-creek:50"


def test_english_art_is_chosen_over_other_languages():
    from hub.tvexplore import english_logo, english_title_card
    logos = [
        {"iso_639_1": "ja", "file_path": "/ja.png", "vote_average": 10, "vote_count": 4},
        {"iso_639_1": "en", "file_path": "/low.png", "vote_average": 1, "vote_count": 1},
        {"iso_639_1": "en", "file_path": "/high.png", "vote_average": 8, "vote_count": 3},
        {"iso_639_1": None, "file_path": "/plain.png", "vote_average": 9, "vote_count": 9},
    ]
    assert english_logo(logos) == "/high.png"
    assert english_logo([{"iso_639_1": "fr", "file_path": "/fr.png"}]) is None
    assert english_logo([{"iso_639_1": None, "file_path": "/plain.png"}]) == "/plain.png"
    backs = [
        {"iso_639_1": None, "file_path": "/clean.jpg", "vote_average": 9},
        {"iso_639_1": "en", "file_path": "/titled.jpg", "vote_average": 3},
        {"iso_639_1": "fr", "file_path": "/fr.jpg", "vote_average": 10},
    ]
    assert english_title_card(backs) == "/titled.jpg"
    assert english_title_card([{"iso_639_1": None, "file_path": "/clean.jpg"}]) is None


def test_provider_names_and_the_plex_library_count_as_owned(hub, people):
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 200, "name": "EN - The Shawshank Redemption (1994)", "category_id": "1"},
    ], vod_categories=[{"category_id": "1", "category_name": "Drama"}])
    hub.db.kv_set("tv.plex.index", [
        {"kind": "movie", "id": "777", "title": "The Godfather", "year": 1972, "tmdb": 238, "rating": "R"},
        {"kind": "series", "id": "778", "title": "Bluey", "year": 2018, "tmdb": None, "rating": "TV-Y"},
    ])
    lists = {
        "new": [{"tmdb_id": 278, "kind": "movie", "title": "The Shawshank Redemption", "year": 1994, "rating": "R"}],
        "popular": [{"tmdb_id": 238, "kind": "movie", "title": "The Godfather", "year": 1972, "rating": "R"},
                    {"tmdb_id": 82728, "kind": "series", "title": "Bluey", "year": 2018, "rating": "TV-Y"}],
        "acclaimed": [{"tmdb_id": 1, "kind": "movie", "title": "Not Here", "year": 2020, "rating": "PG"}],
    }
    publish(hub, lists, can_request=True)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    cards = {item["title"]: item for row in body["rows"] for item in row["items"]}
    assert cards["The Shawshank Redemption"]["availability"] == "play"
    assert cards["The Shawshank Redemption"]["movie_id"] == "vod:thomas-creek:200"
    assert cards["The Godfather"]["availability"] == "play" and cards["The Godfather"]["plex_id"] == "777"
    assert cards["Bluey"]["availability"] == "play" and cards["Bluey"]["series_id"] == "series:plex:778"
    assert cards["Not Here"]["availability"] == "request"
    assert all("match_only" not in item for item in cards.values())


def test_plex_index_rows_never_fill_the_library_row(hub, people):
    hub.db.kv_set("tv.plex.index", [{"kind": "movie", "id": "9", "title": "Hidden", "year": 2000, "tmdb": 5}])
    publish(hub, None, can_request=False)
    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/tv/explore", headers=dylan.h).json() == {"hero": None, "rows": []}
