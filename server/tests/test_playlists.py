"""The playlists report: what each provider carries and what the cleaners make of it, counted off the request path."""

from __future__ import annotations

import asyncio
import time

import httpx

from hub import playlists

from .test_admin_page import _sign_in

MAIN = {
    "fetched_at": 1000.0,
    "channels": [
        {"key": "xc:thomas-creek:1", "name": "US| ESPN HD", "category": "US| SPORTS", "epg_id": "espn.us", "logo": "https://img.test/e.png"},
        {"key": "xc:thomas-creek:2", "name": "##### NEWS #####", "category": "US| NEWS"},
        {"key": "xc:thomas-creek:3", "name": "ZZ| Odd Channel", "category": "US| NEWS"},
        {"key": "xc:thomas-creek:4", "name": "ZZ| Other Channel", "category": "US| NEWS"},
        {"key": "xc:thomas-creek:5", "name": "ZZ| Third Channel", "category": "US| NEWS"},
    ],
    "movies": [
        {"id": "vod:thomas-creek:10", "name": "EN - The Matrix (1999)", "tmdb": "603", "category": "|EN| ACTION"},
        {"id": "vod:thomas-creek:11", "name": "NF - District 9 (2009)", "tmdb": "17654", "category": "|EN| ACTION"},
        {"id": "vod:thomas-creek:12", "name": "Batman: Year One (2011)", "category": "|EN| ACTION"},
    ],
    "series": [{"id": "series:thomas-creek:20", "name": "EN - The Office (2005)", "tmdb": "2316", "year": "2005",
                "category": "|EN| COMEDY"}],
    "guide": {"xc:thomas-creek:1": [{"title": "Game"}]},
    "categories": {"live": ["US| NEWS", "US| SPORTS"], "vod": ["|EN| ACTION"], "series": ["|EN| COMEDY"]},
}
OTHER = {
    "provider": "beta", "fetched_at": 2000.0,
    "channels": [{"key": "xc:beta:1", "name": "US: ESPN FHD", "epg_id": "espn.us", "cat": "5"}],
    "movies": [{"id": "vod:beta:1", "name": "QQ - The Matrix (1999)", "tmdb": "603", "cat": "9"},
               {"id": "vod:beta:2", "name": "QQ - Heat (1995)", "tmdb": "949", "cat": "9"},
               {"id": "vod:beta:3", "name": "QQ - Alien (1979)", "tmdb": "348", "cat": "9"}],
    "series": [],
    "categories": {"live": {"5": "USA | SPORTS"}, "vod": {"9": "QQ | FILMS"}},
}


def test_the_report_counts_each_provider_and_what_the_cleaners_leave():
    report = playlists.build([("thomas-creek", MAIN), ("beta", OTHER)], now=5.0)
    main, beta = report["providers"]
    assert main["main"] and not beta["main"]
    # A section header is not a channel; the guide and the guide id are counted apart.
    assert main["live"]["rows"] == 4 and main["live"]["dividers"] == 1
    assert main["live"]["with"]["guide_id"] == 1 and main["live"]["with"]["programmes"] == 1
    assert main["films"]["with"]["tmdb"] == 2 and main["films"]["with"]["category"] == 3
    # "ZZ|" is no tag the channel cleaner knows, so it is still on the card; "US|" comes off.
    assert main["live"]["tag_left"] == 3
    tags = {row["tag"]: row for row in report["tags"]["live"]}
    assert tags["ZZ"]["left"] == {"thomas-creek": 3} and "US" not in tags  # under MIN_TAG_ROWS
    # A title with a colon is not a tagged name.
    assert playlists.lead_tag("Batman: Year One (2011)", live=False) is None
    assert playlists.lead_tag("|EN| ESPN", live=True) == "EN" and playlists.lead_tag("TOP - Sisu", live=False) == "TOP"
    # The other provider's unknown film tag is left on all three cards.
    films = {row["tag"]: row for row in report["tags"]["films"]}
    assert films["QQ"]["rows"] == {"beta": 3} and films["QQ"]["left"] == {"beta": 3}
    assert beta["films"]["tag_left"] == 3
    # The card name and the matching name agree (they are read through one cleaner).
    assert main["films"]["cleaners_differ"] == 0
    # Categories: the main list keeps names on rows, the others an id and a table.
    assert report["categories"]["thomas-creek"]["live"][0] == {
        "raw": "US| NEWS", "label": "News", "country": "US", "rows": 3}
    assert report["categories"]["beta"]["live"] == [
        {"raw": "USA | SPORTS", "label": "Sports", "country": "US", "rows": 1}]
    assert beta["live"]["with"]["category"] == 1
    # Who carries what: one film both have, two only beta has, one only the main provider has.
    films_by = {tuple(row["providers"]): row["count"] for row in report["overlap"]["films"]}
    assert films_by == {("beta", "thomas-creek"): 1, ("beta",): 2, ("thomas-creek",): 1}
    assert {tuple(row["providers"]): row["count"] for row in report["overlap"]["live"]}[("beta", "thomas-creek")] == 1
    assert report["built_at"] == 5.0 and report["version"] == playlists.VERSION


def test_the_report_holds_no_address_or_login():
    doc = {**OTHER, "channels": [{"key": "xc:beta:1", "name": "US: ESPN", "logo": "http://user:pass@cdn.test/x.png"}]}
    text = str(playlists.build([("beta", doc)]))
    assert "pass" not in text and "cdn.test" not in text and "http" not in text


def test_other_providers_keep_category_ids_without_changing_what_is_judged(hub):
    cat = hub.tv_catalog
    doc = cat.ingest_extra(
        "beta", live=[{"stream_id": 1, "name": "ESPN", "category_id": "5"}],
        vod=[{"stream_id": 7, "name": "A Film", "category_id": 9}], series=[],
        categories={"live": [{"category_id": "5", "category_name": "USA | SPORTS"}],
                    "vod": [{"category_id": 9, "category_name": "KIDS"}], "series": None})
    assert doc["channels"][0]["cat"] == "5" and doc["movies"][0]["cat"] == "9"
    assert doc["categories"] == {"live": {"5": "USA | SPORTS"}, "vod": {"9": "KIDS"}}
    # No `category` on the row: nothing that judges a title by its category (kids, groups) reads the new field.
    assert "category" not in doc["movies"][0] and "category" not in doc["channels"][0]
    # Without category lists (an older caller, a test) the rows are exactly as before.
    plain = cat.ingest_extra("beta", live=[{"stream_id": 1, "name": "ESPN", "category_id": "5"}], vod=[], series=[])
    assert plain["channels"] == [{"key": "xc:beta:1", "name": "ESPN", "epg_id": None}] and "categories" not in plain
    # A category list that failed to read keeps the last table.
    kept = cat.ingest_extra("beta", live=[{"stream_id": 1, "name": "ESPN", "category_id": "5"}], vod=[], series=[],
                            keep=doc, categories={"live": None, "vod": None, "series": None})
    assert kept["categories"] == doc["categories"]


def test_a_provider_read_fetches_categories_and_writes_the_report(hub, monkeypatch):
    cat = hub.tv_catalog
    monkeypatch.setattr(cat, "_extra_logins",
                        lambda: {"beta": {"username": "u", "password": "p", "servers": ["https://a"]}})
    monkeypatch.setattr(cat, "fold", lambda doc=None: None)
    req = httpx.Request("GET", "https://a/player_api.php")
    answers = {
        "get_live_streams": [{"stream_id": 2, "name": "US: CNN", "category_id": "3"}],
        "get_vod_streams": [{"stream_id": 4, "name": "EN - Heat (1995)", "tmdb": "949", "category_id": "8"}],
        "get_series": [],
        "get_live_categories": [{"category_id": "3", "category_name": "US | NEWS"}],
        "get_series_categories": [],
    }  # get_vod_categories is missing: that read fails, and the lists still land

    async def api(pid, servers, params, wait_s=45):
        return servers[0], httpx.Response(200, json=answers[params["action"]], request=req)

    monkeypatch.setattr(cat, "_api", api)
    result = asyncio.run(cat.refresh_extras_here())
    assert result["beta"] == "1 channels, 1 films, 0 series"
    assert cat.extra("beta")["categories"] == {"live": {"3": "US | NEWS"}, "series": {}}
    report = cat.playlist_report()
    assert [p["id"] for p in report["providers"]] == ["beta"]
    assert report["categories"]["beta"]["live"][0]["label"] == "News"
    assert report["stamp"] == playlists.stamp(cat._report_docs())
    assert cat.playlist_report_soon() is False  # the report matches the lists: nothing to build


def test_the_endpoint_is_for_admins_and_the_page_draws_the_stored_report(hub, people, home_client, monkeypatch):
    cat = hub.tv_catalog
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    assert home_client.get("/v1/tv/catalog/playlists", headers=theo.h).status_code == 403
    empty = home_client.get("/v1/tv/catalog/playlists", headers=dylan.h).json()
    assert empty["ready"] is False and empty["building"] is False
    monkeypatch.setattr(cat, "_report_docs", lambda: [("thomas-creek", MAIN), ("beta", OTHER)])
    assert cat.write_playlist_report()["providers"]
    body = home_client.get("/v1/tv/catalog/playlists", headers=dylan.h).json()
    assert body["ready"] and not body["building"] and len(body["providers"]) == 2
    _sign_in(people, home_client)
    # The page reads the stored report only: building one on a request would be the bug.
    monkeypatch.setattr(playlists, "build", lambda *a, **k: (_ for _ in ()).throw(AssertionError("built on a request")))
    started = time.monotonic()
    page = home_client.get("/admin/playlists").text
    assert time.monotonic() - started < 1.0
    assert "Playlists" in page and "QQ" in page and "Left in names" in page and "USA | SPORTS" in page
    assert "/admin/playlists" in home_client.get("/admin/watching").text
    # Signed out, the page is the sign-in page and shows nothing.
    home_client.cookies.clear()
    assert "QQ" not in home_client.get("/admin/playlists").text


def test_a_hub_with_lists_and_no_report_counts_them_by_itself(hub, monkeypatch):
    """Nov 21 2026: after an update the live hub had no report until somebody opened the page."""
    cat = hub.tv_catalog
    monkeypatch.setattr(cat, "_report_docs", lambda: [("thomas-creek", MAIN), ("beta", OTHER)])
    assert cat.playlist_report() == {}

    async def start() -> bool:
        loop = asyncio.create_task(cat.playlist_report_loop(first_s=0, every_s=3600))
        await asyncio.sleep(0.05)
        await cat._report_task  # the check and the count both run off the loop
        again = cat.playlist_report_soon()  # nothing stale now: no second count
        await cat._report_task
        loop.cancel()
        return again

    assert asyncio.run(start()) is False
    report = cat.playlist_report()
    assert len(report["providers"]) == 2 and report["stamp"] == playlists.stamp(cat._report_docs())
    built_at = report["built_at"]
    assert asyncio.run(start()) is False and cat.playlist_report()["built_at"] == built_at
