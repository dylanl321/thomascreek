"""Plex in the TV library: every film and show with ids, episodes, and direct play behind a pass."""

from __future__ import annotations

import httpx
import pytest
import respx

PLEX = "http://plex.test"

MOVIES = {"MediaContainer": {"Metadata": [
    {"ratingKey": "42", "type": "movie", "title": "WALL-E", "year": 2008, "contentRating": "G",
     "Guid": [{"id": "tmdb://10681"}, {"id": "imdb://tt0910970"}], "thumb": "/library/metadata/42/thumb/1",
     "Media": [{"videoResolution": "1080"}], "duration": 5880000, "addedAt": 1700000000},
    {"ratingKey": "43", "type": "movie", "title": "Alien", "year": 1979, "contentRating": "R",
     "Guid": [{"id": "tmdb://348"}], "Media": [{"videoResolution": "4k"}]},
]}}
SHOWS = {"MediaContainer": {"Metadata": [
    {"ratingKey": "70", "type": "show", "title": "Bluey", "year": 2018, "contentRating": "TV-Y",
     "Guid": [{"id": "tmdb://82728"}], "thumb": "/library/metadata/70/thumb/1"},
]}}
LEAVES = {"MediaContainer": {"Metadata": [
    {"ratingKey": "701", "type": "episode", "title": "Magic Xylophone", "grandparentTitle": "Bluey",
     "parentIndex": 1, "index": 1, "duration": 420000},
]}}


@pytest.fixture
def plex(hub):
    hub.plex.cfg.enabled = True
    hub.plex.cfg.url = PLEX
    hub.config.child_policy.ratings = ["G", "TV-Y"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/sections").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Directory": [
            {"key": "1", "type": "movie", "title": "Movies"}, {"key": "2", "type": "show", "title": "TV"},
            {"key": "3", "type": "artist", "title": "Music"}]}}))
        mock.get(f"{PLEX}/library/sections/1/all").mock(return_value=httpx.Response(200, json=MOVIES))
        mock.get(f"{PLEX}/library/sections/2/all").mock(return_value=httpx.Response(200, json=SHOWS))
        mock.get(f"{PLEX}/library/metadata/42").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {**MOVIES["MediaContainer"]["Metadata"][0], "viewOffset": 60000,
             "Media": [{"videoResolution": "1080", "Part": [{"key": "/library/parts/9/file.mkv", "container": "mkv"}]}]}]}}))
        mock.get(f"{PLEX}/library/metadata/43").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {**MOVIES["MediaContainer"]["Metadata"][1], "Media": [{"Part": [{"key": "/library/parts/10/a.mkv"}]}]}]}}))
        mock.get(f"{PLEX}/library/metadata/70").mock(return_value=httpx.Response(200, json=SHOWS))
        mock.get(f"{PLEX}/library/metadata/70/allLeaves").mock(return_value=httpx.Response(200, json=LEAVES))
        mock.get(f"{PLEX}/library/parts/9/file.mkv").mock(return_value=httpx.Response(
            206, content=b"abc", headers={"content-range": "bytes 0-2/100", "content-type": "video/x-matroska"}))
        mock.get(f"{PLEX}/:/timeline").mock(return_value=httpx.Response(200))
        yield mock


def test_the_library_lists_films_and_shows_with_ids(plex, people):
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/plex/library", headers=dylan.h).json()
    films = {m["title"]: m for m in body["movies"]}
    assert films["WALL-E"]["tmdb"] == "10681" and films["WALL-E"]["imdb"] == "tt0910970"
    assert films["WALL-E"]["resolution"] == "1080p" and films["Alien"]["resolution"] == "2160p"
    assert films["WALL-E"]["poster_url"] == "/v1/art/poster/vod:plex:42"
    assert body["shows"][0]["title"] == "Bluey" and body["shows"][0]["kind"] == "series"
    assert "token" not in str(body).lower()


def test_a_child_gets_only_rated_titles(plex, people):
    theo = people["phones"]["Theo"]
    body = theo.client.get("/v1/tv/plex/library", headers=theo.h).json()
    assert [m["title"] for m in body["movies"]] == ["WALL-E"]
    assert theo.client.post("/v1/tv/plex/43/play", headers=theo.h).status_code == 404


def test_episodes_and_direct_play(plex, people):
    dylan = people["phones"]["Dylan"]
    eps = dylan.client.get("/v1/tv/plex/70/episodes", headers=dylan.h).json()["episodes"]
    assert eps == [{"id": "701", "season": 1, "episode": 1, "title": "Magic Xylophone", "duration_s": 420, "offset_s": None}]
    play = dylan.client.post("/v1/tv/plex/42/play", headers=dylan.h).json()
    assert play["url"].startswith("/v1/tv/plex/file/") and play["offset_s"] == 60 and play["container"] == "mkv"
    got = dylan.client.get(play["url"], headers={"Range": "bytes=0-2"})
    assert got.status_code == 206 and got.content == b"abc" and got.headers["accept-ranges"] == "bytes"
    assert got.headers["content-type"] == "video/x-matroska"
    sent = plex.calls.last.request
    assert sent.headers.get("range") == "bytes=0-2"
    assert dylan.client.get("/v1/tv/plex/file/nope").status_code == 401
    assert dylan.client.post("/v1/tv/plex/abc/play", headers=dylan.h).status_code == 400


def test_progress_reaches_plex(plex, people):
    dylan = people["phones"]["Dylan"]
    assert dylan.client.post("/v1/tv/plex/42/progress", headers=dylan.h, json={"offset_s": 90, "duration_s": 5880}).json() == {"ok": True}
    assert plex.calls.last.request.url.params["time"] == "90000"


def test_a_grown_up_watching_as_a_child_gets_only_rated_titles(plex, people, hub):
    dylan = people["phones"]["Dylan"]
    theo = next(p for p in hub.db.people() if p["name"] == "Theo")
    assert dylan.client.post(f"/v1/tv/plex/43/play?person={theo['id']}", headers=dylan.h).status_code == 404
    assert dylan.client.post(f"/v1/tv/plex/42/play?person={theo['id']}", headers=dylan.h).status_code == 200
    assert dylan.client.post("/v1/tv/plex/43/play", headers=dylan.h).status_code == 200


# -- kept for the house (review P3, Nov 21 2026) ------------------------------------------------------------------
def _sections(plex):
    return sum(1 for call in plex.calls if call.request.url.path == "/library/sections")


def test_the_library_is_kept_on_disk_and_answered_without_plex_after_a_restart(plex, people, hub):
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/plex/library", headers=dylan.h)
    assert first.status_code == 200 and _sections(plex) == 1
    assert hub.plex_library.path is not None and hub.plex_library.path.exists()
    # A new hub process: the saved library answers, Plex is not asked while it is fresh.
    from hub.tvplex import PlexLibrary

    hub.plex_library = PlexLibrary(hub, hub.plex_library.path)
    again = dylan.client.get("/v1/tv/plex/library", headers=dylan.h)
    assert again.json() == first.json() and _sections(plex) == 1
    tag = again.headers["etag"]
    assert dylan.client.get("/v1/tv/plex/library", headers={**dylan.h, "If-None-Match": tag}).status_code == 304


def test_an_old_library_answers_at_once_and_refreshes_once_behind(plex, hub):
    import asyncio
    import time as _time

    from hub import tvplex

    async def run():
        lib = hub.plex_library
        doc = await lib.load()
        lib._at = _time.time() - tvplex.TTL_S - 1
        got = await asyncio.gather(*(lib.load() for _ in range(5)))
        assert all(g is doc for g in got)  # nobody waited for Plex
        assert lib._task is not None
        await lib._task
        return lib

    lib = asyncio.run(run())
    assert _sections(plex) == 2 and _time.time() - lib._at < 5


def test_first_callers_share_one_plex_read(plex, hub):
    import asyncio

    async def run():
        return await asyncio.gather(*(hub.plex_library.load() for _ in range(4)))

    got = asyncio.run(run())
    assert all(g is got[0] for g in got) and _sections(plex) == 1


def test_a_child_gets_a_kids_version_and_tag(plex, people):
    theo = people["phones"]["Theo"]
    got = theo.client.get("/v1/tv/plex/library", headers=theo.h)
    assert got.json()["version"].endswith("k") and got.headers["etag"] == f'"{got.json()["version"]}"'
