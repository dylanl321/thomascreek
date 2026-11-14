"""Explore only offers a title the house can play or ask for."""

from __future__ import annotations

from hub.routes.home import shape_event
from hub.tvexplore import advance, publish

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
            "rating": "R", "genres": ["Action", "Science Fiction"],
            "reason": "New this week", "badges": ["New"], "availability": "play",
            "movie_id": "vod:thomas-creek:100",
        }]},
        {"id": "popular", "title": "Popular", "items": [{
            "kind": "series", "title": "Bluey", "year": 2018, "overview": "A puppy.", "tagline": None,
            "poster_url": "/v1/tv/explore/art/poster/tv/82728",
            "backdrop_url": "/v1/tv/explore/art/backdrop/tv/82728",
            "logo_url": None, "rating": "TV-Y", "genres": ["Kids"],
            "reason": "Popular this week", "badges": ["Popular", "Top 10"], "availability": "request",
            "tmdb_id": 82728, "media_id": "tv:82728",
        }]},
        {"id": "acclaimed", "title": "Must watch", "items": [{
            "kind": "movie", "title": "Spirited Away", "year": 2001, "overview": "A girl.", "tagline": None,
            "poster_url": "/v1/tv/explore/art/poster/movie/129",
            "backdrop_url": "/v1/tv/explore/art/backdrop/movie/129",
            "logo_url": None, "rating": "PG", "genres": ["Animation"],
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
        "logo_url": None, "rating": "G", "genres": ["Kids"], "reason": "In your library", "badges": ["New"],
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
