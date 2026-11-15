"""The basic cable lineup: local stations of the home market plus one copy of each network."""

from __future__ import annotations

from hub.tvbasic import build, clean, market_for

RALEIGH = (35.9, -78.8)

ROWS = [
    {"key": "xc:tc:1", "name": "US| ABC 11 RALEIGH WTVD HD", "category": "US| LOCALS"},
    {"key": "xc:tc:2", "name": "US| NBC 5 RALEIGH (WRAL)", "category": "US| LOCALS"},
    {"key": "xc:tc:3", "name": "US| CBS 17 RALEIGH SD", "category": "US| LOCALS"},
    {"key": "xc:tc:4", "name": "US| CBS 17 RALEIGH HD", "category": "US| LOCALS"},
    {"key": "xc:tc:5", "name": "US| FOX 50 RALEIGH", "category": "US| LOCALS"},
    {"key": "xc:tc:6", "name": "US| ABC 7 NEW YORK", "category": "US| LOCALS"},
    {"key": "xc:tc:7", "name": "US| PBS NC RALEIGH", "category": "US| LOCALS"},
    {"key": "xc:tc:10", "name": "US| ESPN HD", "category": "US| SPORTS"},
    {"key": "xc:tc:11", "name": "US| ESPN 2", "category": "US| SPORTS"},
    {"key": "xc:tc:12", "name": "US| ESPN NEWS", "category": "US| SPORTS"},
    {"key": "xc:tc:13", "name": "US| CNN", "category": "US| NEWS"},
    {"key": "xc:tc:14", "name": "US| CNN HD", "category": "US| NEWS"},
    {"key": "xc:tc:15", "name": "US| FOX NEWS", "category": "US| NEWS"},
    {"key": "xc:tc:16", "name": "US| HBO WEST", "category": "US| PREMIUM"},
    {"key": "xc:tc:17", "name": "US| HBO EAST", "category": "US| PREMIUM"},
    {"key": "xc:tc:18", "name": "US| HBO 2", "category": "US| PREMIUM"},
    {"key": "xc:tc:19", "name": "US| DISNEY JR", "category": "US| KIDS"},
    {"key": "xc:tc:20", "name": "US| DISNEY CHANNEL", "category": "US| KIDS"},
    {"key": "xc:tc:21", "name": "UK| BBC ONE", "category": "UK| ENTERTAINMENT"},
    {"key": "xc:tc:22", "name": "US| ESPN 4K", "category": "US| 4K"},
    {"key": "xc:tc:23", "name": "US| HGTV", "category": "US| ENTERTAINMENT"},
]


def _by_name(lineup):
    return {row["name"]: row for row in lineup["channels"]}


def test_clean_drops_the_country_prefix():
    assert clean("US| ESPN HD") == "espn hd"
    assert clean("USA: CNN") == "cnn"
    assert clean("USA Network") == "usa network"


def test_the_nearest_market_with_locals_wins():
    assert market_for(RALEIGH, ROWS)["name"] == "Raleigh"
    assert market_for((40.7, -74.0), ROWS) is None  # only one New York station
    assert market_for(None, ROWS) is None


def test_locals_are_the_home_markets_with_their_own_numbers():
    lineup = build(ROWS, RALEIGH)
    assert lineup["market"] == "Raleigh"
    names = _by_name(lineup)
    assert names["ABC 11 WTVD"]["number"] == 11 and names["ABC 11 WTVD"]["local"]
    assert names["CBS 17"]["key"] == "xc:tc:4"  # HD over SD
    assert names["FOX 50"]["number"] == 50
    assert all(row["key"] != "xc:tc:6" for row in lineup["channels"])  # New York's ABC is not ours


def test_one_copy_of_each_network_in_cable_order():
    lineup = build(ROWS, RALEIGH)
    names = _by_name(lineup)
    assert names["ESPN"]["key"] == "xc:tc:10"
    assert names["ESPN2"]["key"] == "xc:tc:11"
    assert names["ESPNews"]["key"] == "xc:tc:12"
    assert names["CNN"]["key"] == "xc:tc:14"
    assert names["HBO"]["key"] == "xc:tc:17"
    assert names["Disney Channel"]["key"] == "xc:tc:20"
    assert names["Disney Junior"]["key"] == "xc:tc:19"
    assert "xc:tc:21" not in {row["key"] for row in lineup["channels"]}
    assert "xc:tc:22" not in {row["key"] for row in lineup["channels"]}
    numbers = [row["number"] for row in lineup["channels"]]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


def test_a_copy_with_a_guide_beats_one_without():
    lineup = build(ROWS, RALEIGH, has_guide=lambda key: key == "xc:tc:13")
    assert _by_name(lineup)["CNN"]["key"] == "xc:tc:13"


def test_channels_with_no_guide_are_left_out():
    guided = {"xc:tc:1", "xc:tc:2", "xc:tc:4", "xc:tc:5", "xc:tc:10", "xc:tc:14"}
    lineup = build(ROWS, RALEIGH, has_guide=lambda key: key in guided)
    assert {row["key"] for row in lineup["channels"]} == guided
    # Too little guide to go on (not loaded yet): the whole lineup stays
    assert len(build(ROWS, RALEIGH, has_guide=lambda key: key == "xc:tc:13")["channels"]) > 5


def test_the_route_serves_the_basic_lineup(hub, people, monkeypatch):
    hub.tv_catalog.ingest(live=[
        {"stream_id": 1, "name": "US| ABC 11 RALEIGH WTVD HD", "category_id": "1"},
        {"stream_id": 2, "name": "US| NBC 5 RALEIGH", "category_id": "1"},
        {"stream_id": 10, "name": "US| ESPN HD", "category_id": "2"},
    ], live_categories=[{"category_id": "1", "category_name": "US| LOCALS"},
                        {"category_id": "2", "category_name": "US| SPORTS"}])

    async def home():
        return RALEIGH
    monkeypatch.setattr(hub.smart.weather, "_location", home)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/basic", headers=dylan.h)
    assert body.status_code == 200, body.text
    got = body.json()
    assert got["market"] == "Raleigh"
    assert [(row["number"], row["name"]) for row in got["channels"]] == [(5, "NBC 5"), (11, "ABC 11 WTVD"), (30, "ESPN")]
    assert dylan.client.get("/v1/tv/basic", headers={**dylan.h, "If-None-Match": body.headers["etag"]}).status_code == 304
