from __future__ import annotations

import time

import httpx
import pytest
import respx

from hub.db import new_id
from hub.routes.media_requests import derive, push_ready

SEERR = "http://seerr.test"

MOVIE_PG = {"id": 11, "title": "Flow", "releaseDate": "2024-10-25", "posterPath": "/flow.jpg", "overview": "A cat.",
            "releases": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "PG"}]}]}}
MOVIE_R = {"id": 12, "title": "Alien", "releaseDate": "1979-05-25",
           "releases": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "R"}]}]}}
SHOW = {"id": 21, "name": "Severance", "firstAirDate": "2022-02-18",
        "contentRatings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
        "seasons": [{"seasonNumber": 0, "name": "Specials"}, {"seasonNumber": 1, "name": "Season 1", "episodeCount": 9},
                    {"seasonNumber": 2, "name": "Season 2", "episodeCount": 10}],
        "mediaInfo": {"status": 4, "ratingKey": 900, "seasons": [{"seasonNumber": 1, "status": 5}]}}

future = "2099-01-01T00:00:00Z"
REQS = [
    # made in the app by Theo (hub row below), still looking
    {"id": 101, "status": 2, "type": "movie", "createdAt": "2026-11-12T18:00:00Z",
     "media": {"mediaType": "movie", "tmdbId": 11, "status": 3}, "requestedBy": {"displayName": "Admin"}},
    # made on the Jellyseerr site by Avery, season 2 downloading, season 1 on Plex
    {"id": 102, "status": 2, "type": "tv", "createdAt": "2026-11-12T17:00:00Z",
     "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}], "requestedBy": {"displayName": "Avery Lewis"},
     "media": {"mediaType": "tv", "tmdbId": 21, "status": 4, "ratingKey": 900,
               "seasons": [{"seasonNumber": 1, "status": 5}, {"seasonNumber": 2, "status": 3}],
               "downloadStatus": [{"size": 100, "sizeLeft": 40, "estimatedCompletionTime": future,
                                   "episode": {"seasonNumber": 2, "episodeNumber": 3}}]}},
    # someone unknown, ready
    {"id": 103, "status": 5, "type": "movie", "createdAt": "2026-11-10T17:00:00Z",
     "media": {"mediaType": "movie", "tmdbId": 12, "status": 5, "ratingKey": 777}, "requestedBy": {"displayName": "Guest"}},
]


@pytest.fixture
def seerr(hub, people):
    hub.config.jellyseerr.enabled, hub.config.jellyseerr.url = True, SEERR
    hub.config.hub.dry_run = False
    hub.jellyseerr._details.clear()
    hub.jellyseerr._broken.clear()
    hub.db.insert("requests", {"id": new_id("req"), "created_at": time.time(), "kind": "media",
                               "requester_id": people["theo"]["id"], "title": "Flow", "payload": {},
                               "status": "approved", "external_id": "101"})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SEERR}/api/v1/request").mock(return_value=httpx.Response(200, json={"results": REQS}))
        for r in REQS:
            mock.get(f"{SEERR}/api/v1/request/{r['id']}").mock(return_value=httpx.Response(200, json=r))
        mock.get(f"{SEERR}/api/v1/movie/11").mock(return_value=httpx.Response(200, json=MOVIE_PG))
        mock.get(f"{SEERR}/api/v1/movie/12").mock(return_value=httpx.Response(200, json=MOVIE_R))
        mock.get(f"{SEERR}/api/v1/tv/21").mock(return_value=httpx.Response(200, json=SHOW))
        yield mock


def test_derive_statuses():
    assert derive({"status": 1, "media": {"status": 2}})["status"] == "waiting"
    assert derive({"status": 2, "media": {"status": 3}})["status"] == "searching"
    assert derive({"status": 4, "media": {"status": 3}})["status"] == "failed"
    assert derive({"status": 3, "media": {}})["status"] == "declined"
    assert derive({"status": 5, "media": {"status": 5}})["status"] == "ready"
    d = derive(REQS[1])
    assert d["status"] == "downloading" and d["progress"] == 0.6 and d["eta_s"] > 0
    assert [s["status"] for s in d["seasons"]] == ["ready", "downloading"]
    done = derive({"status": 2, "seasons": [{"seasonNumber": 1}],
                   "media": {"status": 4, "seasons": [{"seasonNumber": 1, "status": 5}, {"seasonNumber": 2, "status": 1}]}})
    assert done["status"] == "ready"  # every season they asked for is there


def test_everyone_sees_all_requests_with_owner_and_progress(seerr, people, home_client):
    r = home_client.get("/v1/media/requests?scope=all", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert items["101"]["who"] == "Theo" and items["101"]["status"] == "searching" and items["101"]["title"] == "Flow"
    assert items["102"]["who"] == "Avery" and items["102"]["via"] == "Jellyseerr" and items["102"]["progress"] == 0.6
    assert items["103"]["who"] == "Guest" and items["103"]["status"] == "ready" and items["103"]["plex_id"] == "777"
    mine = home_client.get("/v1/media/requests?scope=mine", headers=people["phones"]["Avery"].h).json()["items"]
    assert [i["id"] for i in mine] == ["102"]
    ready = home_client.get("/v1/media/requests?scope=all&filter=ready", headers=people["phones"]["Dylan"].h).json()
    assert {i["id"] for i in ready["items"]} == {"103"}  # 102 is still downloading season 2


def test_child_only_sees_own_requests(seerr, people, home_client):
    theo = people["phones"]["Theo"]
    items = home_client.get("/v1/media/requests?scope=all", headers=theo.h).json()["items"]
    assert [i["id"] for i in items] == ["101"]
    assert home_client.get("/v1/media/requests/102", headers=theo.h).status_code == 404
    one = home_client.get("/v1/media/requests/101", headers=theo.h).json()
    assert one["can_cancel"] and not one["can_retry"]


def test_child_asks_without_approval_but_only_for_allowed_ratings(hub, seerr, people, home_client):
    seerr.post(f"{SEERR}/api/v1/request").mock(return_value=httpx.Response(201, json={"id": 104}))
    seerr.get(f"{SEERR}/api/v1/request/104").mock(return_value=httpx.Response(200, json={
        "id": 104, "status": 2, "media": {"mediaType": "movie", "tmdbId": 11, "status": 2}}))
    theo = people["phones"]["Theo"]
    r = home_client.post("/v1/media/requests", json={"media_id": "movie:12"}, headers=theo.h)
    assert r.status_code == 403 and r.json()["ask_parent"] is True
    r = home_client.post("/v1/media/requests", json={"media_id": "movie:11"}, headers=theo.h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "104" and r.json()["who"] == "Theo" and r.json()["status"] == "searching"
    assert hub.db.one("SELECT requester_id FROM requests WHERE external_id='104'")["requester_id"] == people["theo"]["id"]
    sent = [c.request for c in seerr.calls if c.request.method == "POST"]
    assert sent and b'"mediaId":11' in sent[-1].content.replace(b" ", b"")


def test_already_on_plex_is_refused(seerr, people, home_client):
    seerr.get(f"{SEERR}/api/v1/movie/12").mock(return_value=httpx.Response(200, json={**MOVIE_R, "mediaInfo": {"status": 5}}))
    r = home_client.post("/v1/media/requests", json={"media_id": "movie:12"}, headers=people["phones"]["Dylan"].h)
    assert r.status_code == 409 and r.json()["error"] == "already_available"


def test_show_seasons_are_sent(seerr, people, home_client):
    route = seerr.post(f"{SEERR}/api/v1/request").mock(return_value=httpx.Response(201, json={"id": 105}))
    seerr.get(f"{SEERR}/api/v1/request/105").mock(return_value=httpx.Response(404))
    r = home_client.post("/v1/media/requests", json={"media_id": "tv:21", "seasons": [2]}, headers=people["phones"]["Avery"].h)
    assert r.status_code == 200, r.text
    assert b'"seasons":[2]' in route.calls[-1].request.content.replace(b" ", b"")


def test_title_and_discover_badges(seerr, people, home_client):
    seerr.get(f"{SEERR}/api/v1/search").mock(return_value=httpx.Response(200, json={"results": [
        {"id": 11, "mediaType": "movie", "title": "Flow", "releaseDate": "2024-01-01", "mediaInfo": {"status": 5, "ratingKey": 5}},
        {"id": 12, "mediaType": "movie", "title": "Alien", "mediaInfo": {"status": 3, "requests": [{"id": 9}]}},
        {"id": 99, "mediaType": "person", "name": "Someone"}]}))
    d = home_client.get("/v1/media/discover?q=f", headers=people["phones"]["Dylan"].h).json()["items"]
    assert [(i["title"], i["availability"]) for i in d] == [("Flow", "ready"), ("Alien", "requested")]
    assert d[1]["request_id"] == "9"
    kid = home_client.get("/v1/media/discover?q=f", headers=people["phones"]["Theo"].h).json()["items"]
    assert [i["title"] for i in kid] == ["Flow"]
    t = home_client.get("/v1/media/title/tv:21", headers=people["phones"]["Dylan"].h).json()
    assert t["rating"] == "TV-MA" and [s["n"] for s in t["seasons"]] == [1, 2] and t["seasons"][0]["status"] == "ready"
    assert t["availability"] == "partial" and t["plex_id"] == "900"
    assert home_client.get("/v1/media/title/tv:21", headers=people["phones"]["Theo"].h).status_code == 404


def test_cancel_rules(seerr, people, home_client):
    seerr.delete(f"{SEERR}/api/v1/request/101").mock(return_value=httpx.Response(204))
    assert home_client.delete("/v1/media/requests/102", headers=people["phones"]["Theo"].h).status_code == 403
    assert home_client.delete("/v1/media/requests/102", headers=people["phones"]["Dylan"].h).status_code == 409
    assert home_client.delete("/v1/media/requests/101", headers=people["phones"]["Theo"].h).status_code == 200


async def test_ready_push_once_and_not_for_old_news(hub, people, seerr):
    out = []

    async def send(**kw):
        out.append(kw)

    hub.notify.send = send  # type: ignore[method-assign]
    assert await push_ready(hub) == 0          # 103 was ready before the hub looked: no push
    arrived = [{**REQS[0], "status": 5, "media": {**REQS[0]["media"], "status": 5}}] + REQS[1:]
    seerr.get(f"{SEERR}/api/v1/request").mock(return_value=httpx.Response(200, json={"results": arrived}))
    if True:
        assert await push_ready(hub) == 1      # Theo's Flow arrived
        assert out[0]["title"] == "Flow is ready to watch" and out[0]["person_ids"] == [people["theo"]["id"]]
        assert await push_ready(hub) == 0      # once only
        assert hub.db.one("SELECT status FROM requests WHERE external_id='101'")["status"] == "ready"


def test_one_broken_title_does_not_flag_jellyseerr(hub, seerr, people, home_client):
    # Jellyseerr answers 500 for a film TMDB no longer knows; the list still loads and Home stays quiet.
    seerr.get(f"{SEERR}/api/v1/movie/12").mock(return_value=httpx.Response(500, json={"message": "Unable to retrieve movie."}))
    r = home_client.get("/v1/media/requests?scope=all", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert {i["id"]: i for i in r.json()["items"]}["103"]["title"] == "Unknown title"
    assert hub.jellyseerr.last_error is None
    assert not any(h["error"] for h in hub.health() if h.get("id") == "jellyseerr" or h.get("name") == "jellyseerr")
    calls = len(seerr.calls)
    home_client.get("/v1/media/requests?scope=all", headers=people["phones"]["Dylan"].h)
    assert not any(c.request.url.path == "/api/v1/movie/12" for c in seerr.calls[calls:])  # not asked again for a while
