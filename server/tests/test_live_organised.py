"""Live TV organised for the phone: categories by country and kind with clean labels, and Locals for wherever
the phone is (its location, a market picked by name, or a point on a map)."""

from __future__ import annotations

from hub import tvgroups, tvnames

LIVE = [
    {"stream_id": 1, "name": "US| ABC 11 RALEIGH WTVD HD", "category_id": "1"},
    {"stream_id": 2, "name": "US| NBC 5 RALEIGH", "category_id": "1"},
    {"stream_id": 3, "name": "US| ABC 7 NEW YORK", "category_id": "1"},
    {"stream_id": 4, "name": "US| NBC 4 NEW YORK", "category_id": "1"},
    {"stream_id": 10, "name": "US| ESPN HD", "category_id": "2"},
    {"stream_id": 20, "name": "UK| SKY SPORTS MAIN EVENT", "category_id": "3"},
    {"stream_id": 21, "name": "UK| BBC ONE", "category_id": "4"},
    {"stream_id": 30, "name": "NL| ZIGGO SPORT", "category_id": "5"},
]
CATEGORIES = [{"category_id": "1", "category_name": "US| LOCALS"}, {"category_id": "2", "category_name": "US| SPORTS"},
              {"category_id": "3", "category_name": "UK | SKY SPORTS+ ᵛᴵᴾ"},
              {"category_id": "4", "category_name": "UK I BBC iPLAYER ᵛᴵᴾ"},
              {"category_id": "5", "category_name": "NL | SPORTS MIX 16Kᵁᴴᴰ"}]


def test_provider_group_names_come_out_clean():
    assert tvnames.category("UK | SKY SPORTS+ ᵛᴵᴾ") == "Sky Sports+"
    assert tvnames.category("UK I BBC iPLAYER ᵛᴵᴾ") == "BBC iPlayer"
    assert tvnames.category("NL | SPORTS MIX 16Kᵁᴴᴰ") == "Sports Mix 16K"
    assert tvnames.category("WS | WORLD SPORTS") == "World Sports"
    assert tvnames.category("REPLAY ᵛᴵᴾ") == "Replay"
    assert tvnames.category("FBI - Most Wanted") == "FBI - Most Wanted"
    assert tvgroups.country_of("UK I SKY STORE") == ("UK", "United Kingdom")
    assert tvgroups.country_of("|EN| KIDS") is None and tvgroups.country_of("NL: NEWS") == ("NL", "Netherlands")


def test_groups_by_country_then_kind(hub, people):
    hub.tv_catalog.ingest(live=LIVE, live_categories=CATEGORIES)
    dylan = people["phones"]["Dylan"]
    r = dylan.client.get("/v1/tv/groups", headers=dylan.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["code"] for c in body["countries"]] == ["US", "UK", "NL"]
    us = body["countries"][0]
    assert [(g["label"], g["kind"], g["count"]) for g in us["groups"]] == [
        ("Locals", "locals", 4), ("Sports", "sports", 1)]
    uk = {g["label"]: g["kind"] for g in body["countries"][1]["groups"]}
    assert uk == {"Sky Sports+": "sports", "BBC iPlayer": "entertainment"}
    assert body["kind_labels"]["sports"] == "Sports"
    lineup = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()["channels"]
    assert {row.get("group_country") for row in lineup} == {"US", "UK", "NL"}


def test_locals_for_where_the_phone_is(hub, people):
    hub.tv_catalog.ingest(live=LIVE, live_categories=CATEGORIES)
    dylan = people["phones"]["Dylan"]
    near_nyc = dylan.client.get("/v1/tv/basic", headers=dylan.h, params={"lat": 40.75, "lon": -73.99}).json()
    assert near_nyc["market"] == "New York"
    assert {c["name"] for c in near_nyc["channels"] if c["local"]} == {"ABC 7", "NBC 4"}
    picked = dylan.client.get("/v1/tv/basic", headers=dylan.h, params={"market": "raleigh"}).json()
    assert picked["market"] == "Raleigh"
    assert dylan.client.get("/v1/tv/basic", headers=dylan.h, params={"market": "Atlantis"}).status_code == 404
    assert dylan.client.get("/v1/tv/basic", headers=dylan.h, params={"lat": "x", "lon": "1"}).status_code == 400
    berlin = dylan.client.get("/v1/tv/basic", headers=dylan.h, params={"lat": 52.52, "lon": 13.40}).json()
    assert berlin["market"] is None and all(not c["local"] for c in berlin["channels"])

    markets = dylan.client.get("/v1/tv/markets", headers=dylan.h, params={"lat": 35.9, "lon": -78.8}).json()["markets"]
    assert markets[0]["name"] == "Raleigh" and markets[0]["locals"] == 2 and markets[0]["km"] < 50
    found = dylan.client.get("/v1/tv/markets", headers=dylan.h, params={"q": "york"}).json()["markets"]
    assert [m["name"] for m in found] == ["New York"] and found[0]["locals"] == 2
