"""The "Shuffle a favorite" row: comfort shows the house can play, chosen on the hub."""

from __future__ import annotations

import asyncio
from datetime import date

from hub import rewatch
from hub.tvexplore import publish


def _library(hub):
    hub.tv_app.set_kids_categories(["|EN| KIDS"])
    hub.tv_catalog.ingest(
        series=[
            {"series_id": 1, "name": "EN - Seinfeld (1989)", "category_id": "1"},
            {"series_id": 2, "name": "EN - Seinfeld 4K", "category_id": "1"},
            {"series_id": 3, "name": "|EN| The Office (US)", "category_id": "1"},
            {"series_id": 4, "name": "EN - Bluey", "category_id": "2"},
            {"series_id": 5, "name": "EN - Some Thriller", "category_id": "1"},
        ],
        series_categories=[{"category_id": "1", "category_name": "|EN| COMEDY"},
                           {"category_id": "2", "category_name": "|EN| KIDS"}],
    )


def test_names_match_past_provider_tags():
    assert rewatch.norm("EN - The Office (US)") == rewatch.norm("The Office")
    assert rewatch.norm("Parks & Recreation") == rewatch.norm("Parks and Recreation")
    assert rewatch.norm("NF - Seinfeld 4K (1989)") == "seinfeld"


def test_the_row_has_one_copy_of_each_listed_show_and_starts_a_shuffle(hub, people):
    _library(hub)
    publish(hub, None, can_request=False)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/explore", headers=dylan.h).json()
    row = next(r for r in body["rows"] if r["id"] == "rewatch")
    assert row["title"] == "Shuffle a favorite"
    names = sorted(rewatch.norm(card["title"]) for card in row["items"])
    assert names == ["bluey", "office", "seinfeld"]  # one Seinfeld, no thriller
    assert all(card["play"] == "shuffle" and card["series_id"] for card in row["items"])
    assert all(card["reason"] == "Made for shuffling" for card in row["items"])

    theo = people["phones"]["Theo"]
    kids = theo.client.get("/v1/tv/explore", headers=theo.h).json()
    kid_row = next(r for r in kids["rows"] if r["id"] == "rewatch")
    assert [rewatch.norm(card["title"]) for card in kid_row["items"]] == ["bluey"]


def test_the_order_changes_by_day_not_by_call():
    library = [{"kind": "series", "series_id": f"series:x:{n}", "title": name}
               for n, name in enumerate(["Seinfeld", "Friends", "Frasier", "Cheers", "Scrubs", "Community"])]
    one = [i["title"] for i in rewatch.row(library, list(rewatch.CLASSICS), day=date(2026, 11, 16))]
    again = [i["title"] for i in rewatch.row(library, list(rewatch.CLASSICS), day=date(2026, 11, 16))]
    assert one == again and len(one) == 6
    days = {tuple(i["title"] for i in rewatch.row(library, list(rewatch.CLASSICS), day=date(2026, 11, d)))
            for d in range(1, 10)}
    assert len(days) > 1


def test_the_models_list_is_kept_a_month_and_a_miss_keeps_the_classics(hub):
    async def answer(_h):
        return ["Seinfeld", "Taskmaster"]

    async def miss(_h):
        return []

    assert asyncio.run(rewatch.refresh_titles(hub, ask=miss, now=1000)) is False
    assert rewatch.titles(hub.db)[0] == "Seinfeld" and "Taskmaster" not in rewatch.titles(hub.db)
    assert asyncio.run(rewatch.refresh_titles(hub, ask=answer, now=1000 + rewatch.RETRY_S)) is True
    assert rewatch.titles(hub.db)[:2] == ["Seinfeld", "Taskmaster"]
    assert "Friends" in rewatch.titles(hub.db)
    assert asyncio.run(rewatch.refresh_titles(hub, ask=answer, now=1000 + rewatch.RETRY_S + 60)) is False


def test_only_a_json_list_of_names_is_read():
    assert rewatch.read_titles('Sure! {"titles": ["Friends", "Friends", "", 3]}') == ["Friends"]
    assert rewatch.read_titles("no json here") == []


def test_shows_without_a_picture_are_left_out_of_a_full_row():
    names = ["Seinfeld", "Friends", "Frasier", "Cheers", "Scrubs", "Community", "Monk", "Psych"]
    library = [{"kind": "series", "series_id": f"series:x:{n}", "title": name, "poster_url": f"/art/{n}",
                "art_known": name not in ("Monk", "Psych")} for n, name in enumerate(names)]
    got = [i["title"] for i in rewatch.row(library, list(rewatch.CLASSICS))]
    assert sorted(got) == sorted(names[:6])  # six with pictures: no grey tiles
    few = [item for item in library if item["title"] in ("Seinfeld", "Monk")]
    assert [i["title"] for i in rewatch.row(few, list(rewatch.CLASSICS))] == ["Seinfeld", "Monk"]  # pictures first
    # The copy with a picture wins over one without.
    copies = [{"kind": "series", "series_id": "series:a:1", "title": "Monk", "art_known": False, "poster_url": "/a"},
              {"kind": "series", "series_id": "series:b:1", "title": "Monk", "art_known": True, "poster_url": "/b"}]
    assert rewatch.row(copies, list(rewatch.CLASSICS))[0]["series_id"] == "series:b:1"
