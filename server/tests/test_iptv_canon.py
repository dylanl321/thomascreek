"""Messy provider names fold into one title only when the hub is sure."""

from __future__ import annotations

import hashlib
import json

from hub.iptvcanon import merge, observe, playable, search, split, unsure

MOVIES = [
    {"provider": "alpha", "kind": "movie", "stream_id": "100", "tmdb": "603",
     "name": "US| The Matrix (1999) [4K] HEVC"},
    {"provider": "beta", "kind": "movie", "stream_id": "55", "tmdb": "603",
     "name": "EN - The Matrix 1999 FHD"},
    {"provider": "alpha", "kind": "movie", "stream_id": "101", "tmdb": "604",
     "name": "The Matrix Reloaded (2003) HD"},
    {"provider": "alpha", "kind": "live", "stream_id": "10", "tvg_id": "espn.us",
     "name": "ESPN HD", "logo": "https://img.example/espn.png"},
    {"provider": "beta", "kind": "live", "stream_id": "11", "tvg_id": "espn.us",
     "name": "ESPN FHD", "logo": "https://img.example/espn.png"},
    {"provider": "alpha", "kind": "live", "stream_id": "12", "name": "ESPN 2 HD"},
    {"provider": "alpha", "kind": "live", "stream_id": "13", "name": "ESPN Deportes"},
    {"provider": "alpha", "kind": "live", "stream_id": "14", "name": "CNN East"},
    {"provider": "alpha", "kind": "live", "stream_id": "15", "name": "CNN West"},
    {"provider": "alpha", "kind": "live", "stream_id": "16", "name": "CNN +1"},
    {"provider": "alpha", "kind": "movie", "stream_id": "200", "name": "ᴿᴬᵂ Dune (2021)"},
    {"provider": "beta", "kind": "movie", "stream_id": "201", "name": "Dune 2021 HD", "tmdb": "438631"},
]


def _id(identity: str) -> str:
    return "can_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def test_the_catalog_route_matches_the_contract(hub, people):
    from tests.test_iptv_canon import MOVIES
    observe(hub.db, MOVIES)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/catalog", headers=dylan.h, params={"q": "matrix", "kind": "movie"}).json()
    assert body["items"][0]["title"] == "The Matrix"
    assert body["items"][0]["sources"][0]["quality"] == "2160p"
    assert "password" not in dylan.client.get("/v1/tv/catalog", headers=dylan.h).text
    missing = dylan.client.post("/v1/tv/catalog/merge", headers=dylan.h, json={"ids": ["can_missing", "can_other"]})
    assert missing.status_code == 404 and missing.json()["message"] == "That title isn't in the catalog."
    avery = people["phones"]["Avery"]
    assert avery.client.post("/v1/tv/catalog/merge", headers=avery.h, json={"ids": ["can_a", "can_b"]}).status_code == 403


def test_messy_names_fold_only_when_sure(hub):
    db = hub.db
    observe(db, MOVIES)
    matrix = search(db, "matrix", "movie")["items"]
    assert matrix == [{
        "id": _id("movie|603"),
        "kind": "movie",
        "title": "The Matrix",
        "year": 1999,
        "sources": [
            {"provider": "alpha", "stream_id": "100", "ref": "vod:alpha:100", "quality": "2160p",
             "language": None, "name": "US| The Matrix (1999) [4K] HEVC"},
            {"provider": "beta", "stream_id": "55", "ref": "vod:beta:55", "quality": "1080p",
             "language": "en", "name": "EN - The Matrix 1999 FHD"},
        ],
    }, {
        "id": _id("movie|604"),
        "kind": "movie",
        "title": "The Matrix Reloaded",
        "year": 2003,
        "sources": [
            {"provider": "alpha", "stream_id": "101", "ref": "vod:alpha:101", "quality": "720p",
             "language": None, "name": "The Matrix Reloaded (2003) HD"},
        ],
    }]
    live = {item["title"] + "|" + str(item.get("variant")) + "|" + str(item.get("timeshift")): item
            for item in search(db, "", "live")["items"]}
    espn = live["ESPN|None|0"]
    assert [source["stream_id"] for source in espn["sources"]] == ["10", "11"]
    assert "ESPN|2|0" in live and "ESPN|deportes|0" in live
    assert "CNN|east|0" in live and "CNN|west|0" in live and "CNN|None|1" in live
    assert playable(db, kind="movie", title="The Matrix", year=1999, tmdb="603")["ref"] == "vod:alpha:100"


def test_unsure_pairs_stay_apart_until_someone_says(hub):
    db = hub.db
    sources = [
        {"provider": "alpha", "kind": "movie", "stream_id": "1", "name": "Dune", "year": 2021},
        {"provider": "beta", "kind": "movie", "stream_id": "2", "name": "Dune"},
    ]
    calls: list = []
    observe(db, sources)
    assert len(search(db, "dune")["items"]) == 2
    assert calls == []
    from hub.iptvcanon import _pair_id, _source, ai_payload
    left, right = _source(sources[0]), _source(sources[1])
    assert left and right
    pair_id = _pair_id(left, right)
    sent = ai_payload([{"id": pair_id, "kind": "movie", "left": "Dune", "right": "Dune", "left_year": 2021,
                        "right_year": None, "logo": "https://user:pass@cdn.example/a.png"}])
    assert "pass" not in json.dumps(sent) and "cdn.example" not in json.dumps(sent)
    observe(db, [
        {**sources[0], "name": "Dune (2021)"},
        sources[1],
    ], decisions={pair_id: True})
    assert len(search(db, "dune")["items"]) == 1
    observe(db, [
        {**sources[0], "name": "Dune (2021) HD"},
        sources[1],
    ], decisions={pair_id: False})
    assert len(search(db, "dune")["items"]) == 1 and calls == []


def test_an_admin_merge_wins_and_a_split_is_remembered(hub):
    db = hub.db
    observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "name": "CNN"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "name": "Cable News Network"},
    ])
    rows = search(db, "", "live")["items"]
    assert len(rows) == 2
    merged = merge(db, [rows[0]["id"], rows[1]["id"]])
    assert merged["ok"] is True
    assert len(search(db, "", "live")["items"]) == 1
    again = observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "name": "CNN HD"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "name": "Cable News Network"},
    ])
    assert len(again["items"]) == 1
    parts = split(db, merged["id"], "beta", "2")
    assert parts["ok"] is True and parts["id"] != merged["id"]
    assert len(search(db, "", "live")["items"]) == 2
    observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "tvg_id": "cnn.us", "name": "CNN"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "tvg_id": "cnn.us", "name": "CNN"},
    ])
    assert len(search(db, "", "live")["items"]) == 2
    assert unsure(db)["unsure"] == [] or isinstance(unsure(db)["unsure"], list)
