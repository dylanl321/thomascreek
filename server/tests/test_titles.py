"""Universal title search: one card per film, across Plex, IPTV and Jellyseerr."""

from __future__ import annotations

import pytest

from hub.titles import merge, request_title


def _plex_on(hub, rows):
    hub.plex.cfg.enabled = True
    hub.plex.cfg.url = "http://plex.test"

    async def search_with_ids(q, limit=20):
        return rows

    hub.plex.search_with_ids = search_with_ids


def _seerr_on(hub, rows, details=None, submitted=None):
    hub.jellyseerr.cfg.enabled = True
    hub.jellyseerr.cfg.url = "http://seerr.test"

    async def search_raw(q):
        return rows

    async def fake_details(kind, tmdb, fresh=False):
        return details or {
            "title": "The Matrix", "posterPath": "/m.jpg",
            "releases": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "PG"}]}]},
        }

    async def submit(kind, tmdb, seasons):
        if submitted is not None:
            submitted.append((kind, tmdb))
        return {"id": 44}

    hub.jellyseerr.search_raw = search_raw
    hub.jellyseerr.details = fake_details
    hub.jellyseerr.submit = submit


def _iptv(hub):
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(
        live=[{"stream_id": 7, "name": "ESPN", "num": 8, "category_id": "1", "stream_icon": "https://img.example/espn.png",
               "epg_channel_id": "espn.us", "tv_archive": 1, "tv_archive_duration": 4},
              {"stream_id": 8, "name": "Cartoon Network", "num": 9, "category_id": "2",
               "stream_icon": "http://user:s3cret-tv@img.example/cn.png", "epg_channel_id": "cn.us"}],
        live_categories=[{"category_id": "1", "category_name": "Sports"}, {"category_id": "2", "category_name": "Kids"}],
        vod=[{"stream_id": 100, "name": "The Matrix", "year": "1999", "category_id": "3",
              "stream_icon": "https://img.example/matrix.png"}],
        vod_categories=[{"category_id": "3", "category_name": "Movies"}],
        series=[], series_categories=[],
        xmltv=_guide(),
    )


def _guide() -> bytes:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    def stamp(delta):
        return (now + delta).strftime("%Y%m%d%H%M%S +0000")
    soon = stamp(timedelta(hours=5))
    later = stamp(timedelta(hours=6))
    past = stamp(timedelta(hours=-2))
    past_end = stamp(timedelta(hours=-1))
    return f"""<?xml version="1.0"?><tv>
      <programme start="{soon}" stop="{later}" channel="espn.us"><title>The Matrix</title></programme>
      <programme start="{past}" stop="{past_end}" channel="espn.us"><title>ESPN Tonight</title></programme>
    </tv>""".encode()


@pytest.mark.asyncio
async def test_one_card_merges_plex_iptv_and_tmdb(hub, people):
    _plex_on(hub, [{
        "id": "555", "title": "The Matrix", "year": 1999, "kind": "movie", "rating": "R",
        "poster_url": "/v1/media/art?path=%2Flibrary%2F1", "summary": "A film",
        "tmdb": "603", "imdb": "tt0133093", "tvdb": None, "resolution": "4k",
    }])
    _seerr_on(hub, [{
        "id": 603, "mediaType": "movie", "title": "The Matrix", "releaseDate": "1999-03-31",
        "posterPath": "/m.jpg", "overview": "A film",
        "mediaInfo": {"status": 5, "ratingKey": "555"},
    }])
    _iptv(hub)
    dylan = people["phones"]["Dylan"]
    page = dylan.client.get("/v1/titles/search", headers=dylan.h, params={"q": "matrix"}).json()
    cards = [c for c in page["titles"] if c["title"] == "The Matrix"]
    assert len(cards) == 1, page
    card = cards[0]
    assert card["id"] == "tmdb:movie:603"
    assert card["ids"]["imdb"] == "tt0133093"
    labels = [o["label"] for o in card["offers"]]
    assert "In Plex (4K)" in labels
    assert "IPTV VOD" in labels
    assert any(label.startswith("Airs ") and "ESPN" in label for label in labels)
    plex = next(o for o in card["offers"] if o["source"] == "plex")
    assert plex["plex_id"] == "555" and plex["play_on_phone"] is True
    vod = next(o for o in card["offers"] if o["label"] == "IPTV VOD")
    assert vod["params"]["movie_id"] == "vod:thomas-creek:100"
    assert card["request"]["status"] == "available" and card["request"]["can_request"] is False
    assert "s3cret-tv" not in str(page)


@pytest.mark.asyncio
async def test_catchup_and_live_channel_cards(hub, people):
    _iptv(hub)
    dylan = people["phones"]["Dylan"]
    page = dylan.client.get("/v1/titles/search", headers=dylan.h, params={"q": "espn"}).json()
    sports = next(c for c in page["titles"] if c["title"] == "ESPN Tonight")
    assert sports["offers"][0]["label"].startswith("Catch-up until")
    assert sports["offers"][0]["params"]["kind"] == "catchup"
    espn = next(c for c in page["titles"] if c["kind"] == "channel")
    assert espn["title"] == "ESPN" and espn["offers"][0]["params"]["channel_key"] == "xc:thomas-creek:7"


@pytest.mark.asyncio
async def test_kids_only_see_allowed_ratings_and_ask_a_parent(hub, people):
    _plex_on(hub, [
        {"id": "1", "title": "Bluey", "year": 2018, "kind": "show", "rating": "TV-Y", "tmdb": "82728",
         "imdb": None, "tvdb": None, "resolution": "1080", "poster_url": None, "summary": None},
        {"id": "2", "title": "The Matrix", "year": 1999, "kind": "movie", "rating": "R", "tmdb": "603",
         "imdb": None, "tvdb": None, "resolution": "4k", "poster_url": None, "summary": None},
    ])
    _seerr_on(hub, [
        {"id": 82728, "mediaType": "tv", "title": "Bluey", "firstAirDate": "2018-10-01", "posterPath": "/b.jpg",
         "overview": "A pup", "mediaInfo": {}},
        {"id": 603, "mediaType": "movie", "title": "The Matrix", "releaseDate": "1999-03-31", "posterPath": "/m.jpg",
         "overview": "Nope", "mediaInfo": {}},
    ], details={
        "title": "Bluey",
        "contentRatings": {"results": [{"iso_3166_1": "US", "rating": "TV-Y"}]},
    })

    async def details(kind, tmdb, fresh=False):
        if int(tmdb) == 603:
            return {"title": "The Matrix", "releases": {"results": [
                {"iso_3166_1": "US", "release_dates": [{"certification": "R"}]}]}}
        return {"title": "Bluey", "contentRatings": {"results": [{"iso_3166_1": "US", "rating": "TV-Y"}]}}

    hub.jellyseerr.details = details
    theo = people["phones"]["Theo"]
    page = theo.client.get("/v1/titles/search", headers=theo.h, params={"q": "blue"}).json()
    assert [c["title"] for c in page["titles"]] == ["Bluey"]
    assert page["titles"][0]["request"]["status"] == "available"
    assert page["titles"][0]["request"]["can_request"] is False
    grown = theo.client.post("/v1/titles/request", headers=theo.h, json={"media_id": "movie:603"})
    assert grown.status_code == 403 and grown.json()["ask_parent"] is True
    asked = theo.client.post("/v1/titles/request", headers=theo.h, json={"media_id": "tv:82728"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "waiting"
    row = hub.db.one("SELECT status FROM requests WHERE kind='media' ORDER BY created_at DESC")
    assert row["status"] == "waiting"
    note = hub.db.one("SELECT title FROM inbox WHERE cat='requests' ORDER BY ts DESC")
    assert "Bluey" in note["title"]


@pytest.mark.asyncio
async def test_adult_request_goes_to_jellyseerr(hub, people):
    sent = []
    _seerr_on(hub, [], submitted=sent)
    hub.config.hub.live_actions = ["media.approve"]
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/titles/request", headers=dylan.h, json={"media_id": "movie:603"})
    assert body.status_code == 200, body.text
    assert body.json()["status"] == "requested" and sent == [("movie", 603)]


def test_external_ids_from_plex_guids():
    from hub.adapters.media import _external_ids
    tmdb, imdb, tvdb = _external_ids({
        "guid": "plex://movie/abc",
        "Guid": [{"id": "imdb://tt0133093"}, {"id": "tmdb://603"}, {"id": "com.plexapp.agents.thetvdb://169?lang=en"}],
    })
    assert (tmdb, imdb, tvdb) == ("603", "tt0133093", "169")
