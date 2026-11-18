"""Explore rows and the hero are built on the hub for whoever is watching (tvpicks)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from hub import tvpicks
from hub.tvexplore import publish
from hub.upnext import remember

from tests.test_brain import FakeProvider
from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token

FILMS = [
    # id, name, year, genre, category
    (200, "Heat", 1995, "Crime, Thriller", "1"),
    (201, "Collateral", 2004, "Crime, Thriller", "1"),
    (202, "Ronin", 1998, "Action, Thriller", "1"),
    (203, "Sicario", 2015, "Crime, Thriller", "1"),
    (204, "Zodiac", 2007, "Crime, Mystery", "1"),
    (205, "Prisoners", 2013, "Crime, Thriller", "1"),
    (210, "Arrival", 2016, "Science Fiction, Drama", "1"),
    (211, "Interstellar", 2014, "Sci-Fi & Fantasy, Drama", "1"),
    (212, "Gravity", 2013, "Science Fiction, Thriller", "1"),
    (213, "The Martian", 2015, "Science Fiction, Drama", "1"),
    (214, "Moon", 2009, "Science Fiction, Drama", "1"),
    (215, "Contact", 1997, "Science Fiction, Drama", "1"),
    (216, "La La Land", 2016, "Romance, Music", "1"),
    (217, "Hell or High Water", 2016, "Crime, Western", "1"),
    (206, "Elf", 2003, "Comedy, Family", "4"),
    (207, "The Polar Express", 2004, "Animation, Family", "4"),
    (208, "A Christmas Story", 1983, "Comedy, Family", "4"),
    (209, "The Grinch", 2018, "Animation, Family", "4"),
    (220, "Moana", 2016, "Animation, Family", "4"),
    (221, "Paw Patrol", 2021, "Animation, Kids", "4"),
    (222, "Bluey Movie", 2024, "Animation, Kids", "4"),
    (223, "Toy Story", 1995, "Animation, Family", "4"),
    (224, "Cars", 2006, "Animation, Family", "4"),
    (225, "Up", 2009, "Animation, Adventure", "4"),
    (226, "Coco", 2017, "Animation, Family", "4"),
    (227, "Encanto", 2021, "Animation, Comedy", "4"),
    (228, "Paddington", 2014, "Comedy, Family", "4"),
]
LISTS = {
    "new": [
        {"tmdb_id": 9203, "kind": "movie", "title": "Sicario", "year": 2015, "rating": "R", "genres": ["Crime", "Thriller"],
         "poster_path": "/s.jpg", "backdrop_path": "/sb.jpg"},
        {"tmdb_id": 9213, "kind": "movie", "title": "The Martian", "year": 2015, "rating": "PG-13",
         "genres": ["Science Fiction", "Drama"], "poster_path": "/m.jpg", "backdrop_path": "/mb.jpg"},
    ],
    "popular": [],
    "acclaimed": [],
}
EVENING = datetime(2026, 11, 17, 19, 30)


def _library(hub):
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(vod=[
        {"stream_id": sid, "name": name, "year": str(year), "category_id": cat, "genre": genre, "tmdb": str(9000 + sid),
         **({"rating": "G"} if cat == "4" else {"rating": "R"})}
        for sid, name, year, genre, cat in FILMS
    ], vod_categories=[{"category_id": "1", "category_name": "Films"}, {"category_id": "4", "category_name": "Kids"}])


@pytest.fixture
def house(hub, people, monkeypatch):
    monkeypatch.setattr(hub.policy, "now", lambda: EVENING.replace(tzinfo=hub.policy.tz))
    _library(hub)
    publish(hub, LISTS, can_request=False)
    return people


def _watch(hub, person, sid, title, n=1, profile="adult", tv=None):
    for i in range(n):
        remember(hub.db, profile, {"movie_id": f"vod:thomas-creek:{sid}", "title": title, "position_s": 600},
                 now=EVENING.timestamp() - 3600 * (i + 1), person=person and person["id"], tv=tv)


def _titles(row):
    return [item["title"] for item in row["items"]]


def _rows(body):
    return {row["id"]: row for row in body["rows"]}


def test_two_people_get_their_own_rows_and_hero(hub, house):
    _watch(hub, house["dylan"], 200, "Heat", n=3)
    _watch(hub, house["avery"], 210, "Arrival", n=3)
    dylan, avery = house["phones"]["Dylan"], house["phones"]["Avery"]
    mine = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    hers = avery.client.get("/v1/tv/explore", headers=avery.h).json()

    assert _rows(mine)["because"]["title"] == "Because you watched Heat"
    assert _rows(hers)["because"]["title"] == "Because you watched Arrival"
    assert "Heat" not in _titles(_rows(mine)["because"])
    assert set(_titles(_rows(mine)["because"])) & {"Collateral", "Ronin", "Zodiac", "Prisoners"}
    assert set(_titles(_rows(hers)["because"])) & {"Interstellar", "Gravity", "Moon", "Contact"}
    # New and trending first, leaning to what each one watches.
    assert mine["hero"]["title"] == "Sicario"
    assert hers["hero"]["title"] == "The Martian"
    # Every row says why it's there, and nothing shows twice.
    for body in (mine, hers):
        assert all(row.get("reason") for row in body["rows"])
        keys = [tvpicks.key_of(item) for row in body["rows"] for item in row["items"]]
        assert len(keys) == len(set(keys))
        assert body["curated_by"] == "rules"
    # Evening in November: movie night and the holiday row, from the shared rules.
    assert _rows(mine)["moment"]["title"] == "Movie night" and _rows(mine)["moment"]["part"] == "evening"
    assert _rows(mine)["season"]["title"] == "Holiday movies"
    assert {"Elf", "The Polar Express", "A Christmas Story"} <= set(_titles(_rows(mine)["season"]))
    order = [row["id"] for row in mine["rows"]]
    assert order.index("new") < order.index("because") < order.index("moment")
    # Heroes to step through: the hero first, then other new and trending titles, each with a reason.
    steps = mine["heroes"]
    assert steps[0]["title"] == "Sicario" and len(steps) >= 2
    assert len({tvpicks.key_of(s) for s in steps}) == len(steps) <= tvpicks.HERO_STEPS
    assert all(s.get("reason") for s in steps[1:])


def test_a_child_never_gets_an_adult_title_and_nothing_goes_to_the_model(hub, house):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    fake = FakeProvider()
    hub.brain.router.provider = fake
    theo = house["phones"]["Theo"]
    _watch(hub, house["theo"], 221, "Paw Patrol", n=2, profile="kids")
    _watch(hub, house["theo"], 200, "Heat", n=2, profile="kids")  # a stray adult play must not leak back
    body = theo.client.get("/v1/tv/explore?profile=adult", headers=theo.h).json()
    shown = {item["title"] for row in body["rows"] for item in row["items"]} | {(body["hero"] or {}).get("title")}
    adult = {name for _sid, name, _y, _g, cat in FILMS if cat == "1"}
    assert not shown & adult
    assert {"season", "because"} <= set(_rows(body))
    assert body["curated_by"] == "rules"
    assert fake.requests == []


def test_no_key_still_fills_every_row(hub, house):
    _watch(hub, house["dylan"], 200, "Heat", n=2)
    dylan = house["phones"]["Dylan"]
    asked = []
    hub.explore_curator.maybe_ask = lambda *a: asked.append(a)  # type: ignore[method-assign]
    assert not hub.explore_curator.ready()
    body = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    assert body["curated_by"] == "rules" and {"because", "moment", "season"} <= set(_rows(body))
    assert all(row["reason"] for row in body["rows"])


def test_the_model_words_rows_picks_the_hero_and_is_costed(hub, house):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    _watch(hub, house["dylan"], 200, "Heat", n=3)
    dylan = house["phones"]["Dylan"]
    caught = []
    hub.explore_curator.maybe_ask = lambda who, sit, key: caught.append((who, sit, key))  # type: ignore[method-assign]
    first = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    assert first["curated_by"] == "rules" and len(caught) == 1
    who, sit, key = caught[0]
    assert "Dylan" not in json.dumps(sit)  # titles and genres, no names
    assert [w["title"] for w in sit["watched"]] == ["Heat"]
    hero_n = next(c["n"] for c in sit["hero_from"] if c["title"] == "The Martian")
    picks = [c["n"] for c in sit["pick_from"]][:4][::-1]
    season = [c["n"] for c in sit["season_from"] if c["title"] != "The Grinch"]
    fake = FakeProvider({"reasons": [{"id": "because", "line": "Tense crime stories, like Heat"},
                                     {"id": "top10", "line": "Everyone's watching these"},
                                     {"id": "nope", "line": "ignored"}],
                         "picks": picks + [999], "hero": hero_n, "hero_reason": "A clever survival story for tonight",
                         "season_keep": season, "unsure": False})
    hub.brain.router.provider = fake
    stored = asyncio.run(hub.explore_curator.ask(who, sit, key))
    assert stored and stored["picks"] == picks and stored["hero"] == hero_n

    body = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    rows = _rows(body)
    assert body["curated_by"] == "ai"
    assert rows["because"]["reason"] == "Tense crime stories, like Heat"
    assert rows["because"]["items"][0]["reason"] == "Tense crime stories, like Heat"
    assert body["hero"]["title"] == "The Martian" and body["hero"]["reason"] == "A clever survival story for tonight"
    assert _titles(rows["picks"]) == [sit["pick_from"][n]["title"] for n in picks]
    assert "The Grinch" not in _titles(rows["season"])
    run = hub.db.one("SELECT * FROM ai_runs WHERE job='explore'")
    assert run and run["cost_usd"] > 0 and run["outcome"] == "curated" and run["model"] == "claude-haiku-5-5"
    assert fake.requests[0]["model"] == "claude-haiku-5-5"


def test_a_capped_or_paused_brain_asks_nothing(hub, house):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    assert hub.explore_curator.ready()
    hub.brain.set_paused(True)
    assert not hub.explore_curator.ready()
    hub.brain.set_paused(False)
    hub.config.brain.monthly_cap_usd = 0.01
    hub.brain.log_run(job="layout", trigger="t", cost_usd=0.5)
    assert not hub.explore_curator.ready()
    hub.config.brain.monthly_cap_usd = None
    hub.config.brain.explore_enabled = False
    assert not hub.explore_curator.ready()


def test_a_tv_follows_the_named_person_and_a_named_child_gets_the_kids_feed(hub, house, home_client):
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    headers = _auth(_token(home_client, key, Clock())["access_token"])
    _watch(hub, house["avery"], 210, "Arrival", n=2)
    _watch(hub, None, 200, "Heat", n=2, tv=TV_ID)

    named = home_client.get("/v1/tv/explore", headers=headers, params={"person": house["avery"]["id"]}).json()
    assert _rows(named)["because"]["title"] == "Because you watched Arrival"
    nobody = home_client.get("/v1/tv/explore", headers=headers).json()
    assert _rows(nobody)["because"]["title"] == "Because you watched Heat"
    child = home_client.get("/v1/tv/explore", headers=headers, params={"person": house["theo"]["id"]}).json()
    adult = {name for _sid, name, _y, _g, cat in FILMS if cat == "1"}
    assert not {item["title"] for row in child["rows"] for item in row["items"]} & adult
    stranger = home_client.get("/v1/tv/explore", headers=headers, params={"person": "per_nobody"}).json()
    assert _rows(stranger)["because"]["title"] == "Because you watched Heat"


def test_plain_rules():
    assert tvpicks.genres_of(["Action & Adventure, Sci-Fi", 10765, "Kids"]) == ["action", "adventure",
                                                                                 "science fiction", "kids"]
    assert tvpicks.season_of(datetime(2026, 10, 3).date()) == "halloween"
    assert tvpicks.season_of(datetime(2026, 12, 20).date()) == "holiday"
    assert tvpicks.season_of(datetime(2026, 4, 3).date()) is None
    assert tvpicks.moment_of(datetime(2026, 11, 14, 9)) == "weekend_morning"
    assert tvpicks.moment_of(datetime(2026, 11, 17, 23)) == "night"
    pool = [{"kind": "movie", "title": f"Film {i}", "year": 2016, "genres": ["Drama"], "movie_id": f"m{i}",
             "backdrop_url": "/b"} for i in range(4)]
    rows = tvpicks.plain_rows(pool, [], kids=False, now=datetime(2026, 4, 6, 14), taken=set())
    assert rows["anniversary"]["title"] == "Turning 10 this year" and rows["anniversary"]["years"] == 10
    assert "because" not in rows and "picks" not in rows and "season" not in rows
    family = [{**card, "genres": ["Family"], "title": f"Family {i}", "movie_id": f"f{i}"} for i, card in enumerate(pool)]
    kids_rows = tvpicks.plain_rows(family, [], kids=True, now=datetime(2026, 4, 6, 18), taken=set())
    assert kids_rows["moment"]["title"] == "Family movie night" and "anniversary" not in kids_rows
    answer = tvpicks.clean_answer({"reasons": [{"id": "x", "line": "  a   line "}], "picks": [0, 0, 5, True],
                                   "hero": 7, "hero_reason": "", "season_keep": []},
                                  {"rows": [{"id": "x"}], "pick_from": [{}], "hero_from": [{}], "season_from": []})
    assert answer == {"reasons": {"x": "a line"}, "picks": [0], "hero": None, "hero_reason": None, "season_keep": []}
