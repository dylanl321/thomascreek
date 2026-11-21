"""Hub sport: ESPN games matched to the house's channels (broadcaster on the basic lineup, guide, channel name)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import respx

from hub import tvsport


def _xt(when: datetime) -> str:
    return when.strftime("%Y%m%d%H%M%S +0000")


def _espn_time(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%MZ")


def _nfl(start: datetime, state: str = "in", broadcast: str = "NBC") -> dict:
    def team(ident, name, short, abbr, side, score):
        return {"homeAway": side, "score": score, "team": {"id": ident, "displayName": name, "shortDisplayName": short,
                                                             "abbreviation": abbr, "logo": f"https://a.espncdn.com/{abbr}.png"}}
    return {"events": [{
        "id": "401", "date": _espn_time(start), "name": "Cincinnati Bengals at Pittsburgh Steelers",
        "status": {"type": {"state": state, "name": "STATUS_IN_PROGRESS", "shortDetail": "Q2 4:12"}, "displayClock": "4:12", "period": 2},
        "competitions": [{
            "id": "401", "date": _espn_time(start),
            "status": {"type": {"state": state, "name": "STATUS_IN_PROGRESS", "shortDetail": "Q2 4:12"}, "displayClock": "4:12", "period": 2},
            "venue": {"fullName": "Acrisure Stadium"},
            "broadcasts": [{"market": "national", "names": [broadcast]}],
            "competitors": [team("23", "Pittsburgh Steelers", "Steelers", "PIT", "home", "14"),
                            team("4", "Cincinnati Bengals", "Bengals", "CIN", "away", "10")],
        }],
    }]}


def _catalog(hub, now: datetime) -> None:
    hub.tv_app.set_kids_categories(["Kids"])
    xml = f"""<?xml version="1.0"?>
    <tv>
      <programme start="{_xt(now - timedelta(minutes=40))}" stop="{_xt(now + timedelta(hours=2))}" channel="fs1.us">
        <title>NFL Football: Bengals at Steelers (Alt cast)</title>
      </programme>
      <programme start="{_xt(now - timedelta(minutes=40))}" stop="{_xt(now + timedelta(hours=2))}" channel="news.us">
        <title>Evening News</title>
      </programme>
    </tv>""".encode()
    hub.tv_catalog.ingest(
        live=[
            {"stream_id": 1, "name": "US: NBC 11 WXIA Atlanta", "num": 1, "category_id": "1", "epg_channel_id": "nbc.us"},
            {"stream_id": 2, "name": "US: FS1", "num": 2, "category_id": "2", "epg_channel_id": "fs1.us"},
            {"stream_id": 3, "name": "US: News", "num": 3, "category_id": "1", "epg_channel_id": "news.us"},
            {"stream_id": 4, "name": "NFL 05: Bengals vs Steelers", "num": 4, "category_id": "2"},
            {"stream_id": 5, "name": "Cartoons", "num": 5, "category_id": "3"},
        ],
        live_categories=[{"category_id": "1", "category_name": "US Locals"}, {"category_id": "2", "category_name": "US Sports"},
                         {"category_id": "3", "category_name": "Kids"}],
        vod=[], vod_categories=[], series=[], series_categories=[], xmltv=xml,
    )


def _keys(hub) -> dict[str, str]:
    return {row["name"]: row["key"] for row in hub.tv_catalog.load()["channels"]}


def test_a_network_game_finds_the_local_station_then_the_guide_then_the_event_channel(hub):
    now = datetime.now(timezone.utc)
    _catalog(hub, now)
    keys = _keys(hub)
    basic = {"channels": [
        {"key": keys["US: NBC 11 WXIA Atlanta"], "name": "NBC 11 WXIA", "number": 11, "local": True},
        {"key": keys["US: FS1"], "name": "FS1", "number": 34, "local": False},
    ]}
    games = tvsport.parse(_nfl(now - timedelta(minutes=40)), tvsport.LEAGUES[0])
    [game] = tvsport.match(games, hub.tv_catalog, basic, kids_only=False, now=now.timestamp())
    assert game["home"]["name"] == "Pittsburgh Steelers" and game["state"] == "live" and game["broadcasts"] == ["NBC"]
    assert [(c["name"], c["why"]) for c in game["channels"]] == [
        ("NBC 11 WXIA", "broadcast"), ("FS1", "guide"), ("NFL 05: Bengals vs Steelers", "name")]
    assert game["watch"] == keys["US: NBC 11 WXIA Atlanta"] and game["channels"][0]["number"] == 11


def test_a_kids_caller_gets_the_games_but_no_adult_channels(hub):
    now = datetime.now(timezone.utc)
    _catalog(hub, now)
    keys = _keys(hub)
    basic = {"channels": [{"key": keys["US: NBC 11 WXIA Atlanta"], "name": "NBC 11 WXIA", "number": 11, "local": True}]}
    games = tvsport.parse(_nfl(now), tvsport.LEAGUES[0])
    [game] = tvsport.match(games, hub.tv_catalog, basic, kids_only=True, now=now.timestamp())
    assert game["channels"] == [] and game["watch"] is None


def test_streaming_only_and_unknown_broadcasters_match_nothing(hub):
    now = datetime.now(timezone.utc)
    _catalog(hub, now)
    keys = _keys(hub)
    basic = {"channels": [{"key": keys["US: NBC 11 WXIA Atlanta"], "name": "NBC 11 WXIA", "number": 11, "local": True}]}
    games = tvsport.parse(_nfl(now + timedelta(hours=8), state="pre", broadcast="Prime Video"), tvsport.LEAGUES[0])
    [game] = tvsport.match(games, hub.tv_catalog, basic, kids_only=False, now=now.timestamp())
    assert all(c["why"] != "broadcast" for c in game["channels"])


def test_races_parse_each_session_and_match_their_own_series_only():
    body = {"events": [{"id": "9", "name": "Las Vegas Grand Prix", "date": "2026-11-21T06:00Z",
                        "circuit": {"fullName": "Las Vegas Strip Circuit"},
                        "competitions": [
                            {"id": "91", "date": "2026-11-20T04:00Z", "type": {"abbreviation": "Qual"},
                             "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}}},
                            {"id": "92", "date": "2026-11-21T06:00Z", "type": {"abbreviation": "Race"},
                             "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
                             "broadcasts": [{"names": ["ABC"]}]},
                        ]}]}
    f1 = next(l for l in tvsport.LEAGUES if l[0] == "Formula 1")
    sessions = tvsport.parse(body, f1)
    assert [(s["note"], s["kind"], s["broadcasts"]) for s in sessions] == [("Qualifying", "race", []), ("Race", "race", ["ABC"])]
    assert tvsport._RACE["NASCAR"].search("Formula 1 Grand Prix") is None


@respx.mock
def test_route_answers_from_espn_once_and_ranks_the_live_nfl_game_first(hub, people):
    now = datetime.now(timezone.utc)
    _catalog(hub, now)
    nfl = respx.get(url__startswith=f"{tvsport.ESPN}/football/nfl/").mock(
        return_value=httpx.Response(200, json=_nfl(now - timedelta(minutes=40))))
    respx.get(url__startswith=tvsport.ESPN).mock(return_value=httpx.Response(200, json={"events": []}))
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/sport", headers=dylan.h)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["leagues"][:3] == ["NFL", "NBA", "MLB"]
    assert body["games"][0]["league"] == "NFL" and body["games"][0]["state"] == "live"
    assert any(c["why"] == "guide" for c in body["games"][0]["channels"])
    dylan.client.get("/v1/tv/sport", headers=dylan.h)
    assert nfl.call_count == 1  # kept for a minute while live, not fetched again per screen


def test_scores_are_empty_before_the_start_and_day_boards_ask_for_one_day(hub):
    now = datetime.now(timezone.utc)
    [game] = tvsport.parse(_nfl(now + timedelta(hours=2), state="pre"), tvsport.LEAGUES[0])
    assert game["state"] == "upcoming" and game["home"]["score"] is None and game["away"]["score"] is None
    nba = next(l for l in tvsport.LEAGUES if l[0] == "NBA")
    url = tvsport.Sport(hub)._url(nba, now.timestamp())
    assert url.startswith(f"{tvsport.ESPN}/basketball/nba/scoreboard?dates=") and "-" not in url.split("dates=")[1]


@respx.mock
def test_every_league_is_asked_and_next_weeks_race_weekend_is_kept(hub):
    now = datetime.now(timezone.utc)
    race = {"events": [{"id": "9", "name": "Las Vegas Grand Prix", "date": _espn_time(now + timedelta(days=5)),
                        "competitions": [{"id": "92", "date": _espn_time(now + timedelta(days=5)), "type": {"abbreviation": "Race"},
                                          "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}}}]}]}
    f1 = respx.get(url__startswith=f"{tvsport.ESPN}/racing/f1/").mock(return_value=httpx.Response(200, json=race))
    nba = respx.get(url__startswith=f"{tvsport.ESPN}/basketball/nba/").mock(return_value=httpx.Response(503))
    respx.get(url__startswith=tvsport.ESPN).mock(return_value=httpx.Response(200, json={"events": []}))
    feed = tvsport.Sport(hub)
    import asyncio

    async def run():
        games = await feed.games(now.timestamp())
        assert f1.called and nba.called and [g["league"] for g in games] == ["Formula 1"]
        again = await feed.games(now.timestamp() + 61)
        assert [g["league"] for g in again] == ["Formula 1"]  # answered from the last boards at once
        await feed._refreshing

    asyncio.run(run())
    assert nba.call_count == 2  # a failed board is tried again after a minute, not a quarter hour


@respx.mock
def test_old_scores_answer_at_once_and_refresh_once_behind(hub):
    import asyncio
    import time as _time

    now = datetime.now(timezone.utc).timestamp()
    slow = {"on": False}

    async def answer(request):
        if slow["on"]:
            await asyncio.sleep(0.3)
        return httpx.Response(200, json={"events": []})

    route = respx.get(url__startswith=tvsport.ESPN).mock(side_effect=answer)
    feed = tvsport.Sport(hub)

    async def run():
        await feed.games(now)
        first = route.call_count
        slow["on"] = True
        began = _time.monotonic()
        await asyncio.gather(*(feed.games(now + tvsport.IDLE_TTL_S + 1) for _ in range(4)))
        took = _time.monotonic() - began
        await feed._refreshing
        return first, took

    first, took = asyncio.run(run())
    assert took < 0.2  # nobody waited for ESPN
    assert route.call_count == 2 * first  # one refresh for four callers


def test_boards_come_back_after_a_restart(hub):
    """The last boards are kept on the hub: a new feed (a restart) answers from them at once and refreshes behind,
    instead of waiting on ESPN (Nov 21 2026, 9 s for the first call after a restart)."""
    import asyncio
    import time as _time

    from hub import tvsport

    now = _time.time()
    game = {"id": "g1", "league": tvsport.LEAGUES[0][0], "state": "live", "start": now - 600, "kind": tvsport.TEAM,
            "home": None, "away": None, "broadcasts": []}
    first = tvsport.Sport(hub)
    for league in tvsport.LEAGUES:
        first._boards[league[0]] = (now, [game] if league[0] == game["league"] else [])
    first._save()
    again = tvsport.Sport(hub)
    assert again._boards[game["league"]][1] == [game]
    assert all(again._boards.get(league[0]) is not None for league in tvsport.LEAGUES)
    assert asyncio.run(again.games(now)) == [game]


def test_sport_channels_answer_the_last_match_while_the_next_is_worked_out(hub, monkeypatch):
    """The minute's first /v1/tv/sport gets the last match at once; the new one is worked out behind it."""
    import threading
    import time as _time

    from hub import tvsport
    from hub.routes import tvguide

    gate = threading.Event()
    calls = []

    def match(games, catalog, basic, *, kids_only, now):
        calls.append(now)
        if len(calls) > 1:
            gate.wait(5)
        return [{"n": len(calls)}]

    monkeypatch.setattr(tvsport, "match", match)
    monkeypatch.setattr(hub.tv_catalog, "basic", lambda home: None)
    doc: dict = {"channels": []}
    monkeypatch.setattr(hub.tv_catalog, "load", lambda: doc)
    now = 1_800_000_000.0
    assert tvguide._sport_channels(hub, [], None, False, now) == [{"n": 1}]
    began = _time.perf_counter()
    assert tvguide._sport_channels(hub, [], None, False, now + 61) == [{"n": 1}]  # last answer, not waiting
    assert _time.perf_counter() - began < 1
    gate.set()
    for _ in range(50):
        if hub.__dict__.get("_sport_last", {}).get((id(hub.tv_catalog.load()), False), (0, None))[1] == [{"n": 2}]:
            break
        _time.sleep(0.02)
    assert tvguide._sport_channels(hub, [], None, False, now + 61) == [{"n": 2}]
