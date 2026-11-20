"""Hub fixes from the iPhone walkthrough of Nov 20 2026: kids' rows, row titles, Continue watching and search."""

from __future__ import annotations

from hub import kidratings, tvcatalog, tvgroups, tvnames, upnext
from hub.tvpicks import _clean_title


class _Certs:
    """A certification store that can look titles up (a TMDB key is set) and knows only these."""

    def __init__(self, known):
        self.known = known

    def cert(self, item):
        return self.known.get(item.get("title") or item.get("name"))

    def can_look_up(self):
        return True


def test_a_child_never_gets_a_film_or_show_whose_age_rating_is_not_known_yet():
    certs = _Certs({"Tom and Jerry": "TV-Y7", "Exploding Kittens": "TV-MA"})
    feed = {"rows": [{"id": "because", "items": [
        {"kind": "series", "series_id": "series:p:1", "title": "Tom and Jerry", "rating": "7.9"},
        {"kind": "series", "series_id": "series:p:2", "title": "Exploding Kittens", "rating": "7.0"},
        {"kind": "series", "series_id": "series:p:3", "title": "Dan Da Dan", "rating": "8.4"},  # not looked up yet
        {"kind": "channel", "channel_key": "c1", "title": "Cartoon Classics"},  # a channel: its kids' group decides
    ]}]}
    kept = kidratings.narrow(feed, kidratings.allowed_at("kid"), certs)
    assert [card["title"] for card in kept["rows"][0]["items"]] == ["Tom and Jerry", "Cartoon Classics"]


def test_without_a_tmdb_key_the_kids_groups_are_still_trusted():
    class NoKey(_Certs):
        def can_look_up(self):
            return False

    card = {"kind": "movie", "movie_id": "vod:p:9", "title": "Some Cartoon", "rating": "6.5"}
    assert kidratings.allows_item(kidratings.allowed_at("kid"), card, NoKey({}))


def test_because_you_watched_names_the_show_as_people_read_it():
    assert _clean_title("EN - Community (2009) (US)") == "Community"
    assert _clean_title("EN - Tom And Jerry (2023)") == "Tom And Jerry"
    assert _clean_title("A+ - Fraggle Rock") == "Fraggle Rock"


def test_provider_prefixes_come_off_film_and_channel_names():
    assert tvnames.polish("SD/CAM - Wicked (2024)") == ("Wicked", 2024)
    assert tvnames.polish("Camera Obscura") == ("Camera Obscura", None)
    assert tvnames.channel_name("4K| Sky Sports") == "Sky Sports"
    assert tvnames.channel_name("Prime| Jack Ryan") == "Jack Ryan"
    assert tvnames.channel_name("Prime Video") == "Prime Video"


def test_continue_watching_lists_a_show_once_though_two_providers_carry_it():
    def ep(show, ident):
        return {"kind": "episode", "title": "Episode", "subtitle": show, "episode_id": ident}

    items = [ep("Community", "ep:a:5"), ep("Community", "ep:b:2"), ep("Tom and Jerry", "ep:a:9")]
    row = upnext._row("continue", items, set())
    assert row is not None
    assert [item["episode_id"] for item in row["items"]] == ["ep:a:5", "ep:a:9"]


def test_search_lists_each_film_once_and_names_it_cleanly():
    rows = [
        {"id": "vod:a:1", "name": "EN - Wicked (2024)", "year": "2024"},
        {"id": "vod:b:7", "name": "SD/CAM - Wicked"},
        {"id": "vod:a:2", "name": "EN - Dune (1984)", "year": "1984"},
        {"id": "vod:a:3", "name": "EN - Dune (2021)", "year": "2021"},
    ]
    out = tvcatalog._one_each(rows)
    assert [(row["id"], row["display_name"]) for row in out] == [
        ("vod:a:1", "Wicked"), ("vod:a:2", "Dune"), ("vod:a:3", "Dune")]


def test_live_groups_that_read_the_same_are_listed_once():
    channels = [{"group": "|US| TV", "group_label": "TV"}] * 3 + [{"group": "US| TV HD", "group_label": "TV"}] * 2
    channels += [{"group": "|US| SPORTS", "group_label": "Sports"}]
    us = next(box for box in tvgroups.groups(channels)["countries"] if box["code"] == "US")
    tv = [g for g in us["groups"] if g["label"] == "TV"]
    assert len(tv) == 1 and tv[0]["count"] == 5 and tv[0]["groups"] == ["|US| TV", "US| TV HD"]
