"""New for you: a new episode of a series the person watches, or a requested title that arrived, as a Home card."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from hub import tvexplore
from hub.upnext import remember

from tests.test_smart import subj
from tests.test_upnext import _catalog


def _cards(hub, s, ts):
    return [c for c in hub.smart.cards(s, datetime.fromtimestamp(ts, hub.policy.tz)) if c["rule"] == "new_for_you"]


def _episode(hub, number):
    eps = [{"id": str(899 + n), "episode_num": n, "title": f"Part {n}", "info": {"duration_secs": 1200}}
           for n in range(1, number + 1)]
    hub.tv_catalog.store_series({"info": {"name": "Cartoon Show"}, "episodes": {"1": eps}}, "series:thomas-creek:50")


def test_a_new_episode_shows_once_and_goes_when_it_is_played(hub, people):
    _catalog(hub)
    pid = people["dylan"]["id"]
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    t0 = datetime.now(timezone.utc).timestamp()
    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:901",
                               "title": "Next", "position_s": 1190}, now=t0, person=pid)
    # Everything already known when the hub first looks is old: no flood of cards after a deploy.
    assert _cards(hub, dylan, t0 + 60) == []

    _episode(hub, 3)
    [card] = _cards(hub, dylan, t0 + 3600)
    assert card["title"] == "Cartoon Show" and card["eyebrow"] == "New episode"
    assert card["why"] == "A new episode of a series you watch"
    play = urlparse(card["buttons"][0]["url"])
    assert play.scheme == "thomascreek" and play.netloc == "play"
    q = parse_qs(play.query)
    assert q["episode_id"] == ["ep:thomas-creek:902"] and q["kind"] == ["episode"] and q["title"] == ["Cartoon Show"]

    # Somebody else's history doesn't make it new for them.
    assert _cards(hub, subj(hub, people["avery"], people["phones"]["Avery"]), t0 + 3600) == []

    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:902",
                               "title": "Part 3", "position_s": 40}, now=t0 + 4000, person=pid)
    assert _cards(hub, dylan, t0 + 4100) == []


def test_the_next_episode_of_an_old_series_is_not_news(hub, people):
    """Started a series today: its later episodes were there all along, so they aren't "new"."""
    _catalog(hub)
    _episode(hub, 3)
    pid = people["dylan"]["id"]
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    t0 = datetime.now(timezone.utc).timestamp()
    from hub.newforyou import observe

    observe(hub, t0 - 86400)
    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:900",
                               "title": "Part 1", "position_s": 1190}, now=t0, person=pid)
    assert _cards(hub, dylan, t0 + 60) == []


def test_an_arrived_request_plays_and_goes_once_started_and_kids_only_get_theirs(hub, people):
    _catalog(hub)
    raw = hub.db.kv_get(tvexplore.KV) or {}
    raw["requests"] = [{"tmdb_id": 7, "kind": "movie", "title": "Drama Movie", "status": "ready", "profile": "adult",
                        "movie_id": "vod:thomas-creek:100"}]
    hub.db.kv_set(tvexplore.KV, raw)
    pid = people["dylan"]["id"]
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    theo = subj(hub, people["theo"], people["phones"]["Theo"])
    t0 = datetime.now(timezone.utc).timestamp()

    [card] = _cards(hub, dylan, t0)
    assert card["title"] == "Drama Movie" and card["eyebrow"] == "New for you"
    q = parse_qs(urlparse(card["buttons"][0]["url"]).query)
    assert q["movie_id"] == ["vod:thomas-creek:100"] and q["kind"] == ["movie"]
    assert _cards(hub, theo, t0) == []  # an adult request never reaches a child's Home

    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 120},
             now=t0 + 10, person=pid)
    assert _cards(hub, dylan, t0 + 20) == []
    # Old news drops off after three days even if nobody played it.
    hub.db.kv_set("newforyou.arrived", {"adult:vod:thomas-creek:100": t0 - 4 * 86400})
    avery = subj(hub, people["avery"], people["phones"]["Avery"])
    assert _cards(hub, avery, t0) == []
