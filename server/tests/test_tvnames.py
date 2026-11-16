"""Hub-cleaned film, series and category names: the screens draw these instead of the provider's tags."""

from __future__ import annotations

from hub import tvnames


def test_provider_tags_quality_and_notes_come_off_titles():
    cases = {
        "EN - The Shawshank Redemption (1994)": ("The Shawshank Redemption", 1994, None, None),
        "NF - A Killer Paradox (2024)": ("A Killer Paradox", 2024, None, "Netflix"),
        "NF - Call Me Chihiro 4K (2023) (JAPANESE MULTI SUB)": ("Call Me Chihiro", 2023, "4K", "Netflix"),
        "NF - Blonde 4K (2022) Marilyn Monroe, TONY CURTIS UNCREDIT": ("Blonde", 2022, "4K", "Netflix"),
        "NF – Wednesday": ("Wednesday", None, None, "Netflix"),
        "EN - The Office (US)": ("The Office", None, None, None),
        "EN - Blade Runner 2049 (2017)": ("Blade Runner 2049", 2017, None, None),
        "|EN| Dune": ("Dune", None, None, None),
        "SPT - UFC 300": ("UFC 300", None, None, None),
    }
    for raw, (name, year, quality, service) in cases.items():
        got = tvnames.title(raw)
        assert (got["name"], got["year"], got["quality"], got["service"]) == (name, year, quality, service), raw
    assert tvnames.title("EN - Shogun (2024) MULTI")["subs"] is True


def test_real_titles_that_look_like_tags_are_left_alone():
    for raw in ("FBI - Most Wanted", "24 - Live Another Day", "2012", "9-1-1"):
        assert tvnames.title(raw)["name"] == raw


def test_category_names_lose_language_tags_and_shouting():
    assert tvnames.category("|EN| ACTORS & DIRECTORS") == "Actors & Directors"
    assert tvnames.category("|MULTI| NETFLIX SERIES 4K") == "Netflix Series 4K"
    assert tvnames.category("|EN| HBO MAX") == "HBO Max"
    assert tvnames.category("|SPT| SPORT UFC") == "Sport UFC"
    assert tvnames.category("WORLDCUP 2022") == "World Cup 2022"
    assert tvnames.category("Kids") == "Kids"


def _catalog(hub):
    hub.tv_app.set_kids_categories(["|EN| KIDS"])
    hub.tv_catalog.ingest(
        live=[], live_categories=[],
        vod=[{"stream_id": 7, "name": "NF - Athlete A 4K (2020)", "category_id": "1"},
             {"stream_id": 8, "name": "EN - Moana 2 (2024)", "category_id": "2"}],
        vod_categories=[{"category_id": "1", "category_name": "|EN| NETFLIX"},
                        {"category_id": "2", "category_name": "|EN| KIDS"}],
        series=[{"series_id": 9, "name": "NF - Squid Game (KR)", "category_id": "3"}],
        series_categories=[{"category_id": "3", "category_name": "|MULTI| NETFLIX SERIES"}],
    )


def test_names_route_answers_by_provider_id_and_304s_when_unchanged(hub, people):
    _catalog(hub)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/names", headers=dylan.h)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["movies"]["7"] == ["Athlete A", 2020, "4K", "Netflix"]
    assert body["series"]["9"][0] == "Squid Game"
    assert body["categories"]["vod"]["|EN| NETFLIX"] == "Netflix"
    assert body["categories"]["series"]["|MULTI| NETFLIX SERIES"] == "Netflix Series"
    again = dylan.client.get("/v1/tv/names", headers={**dylan.h, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_a_child_only_gets_kids_names(hub, people):
    _catalog(hub)
    theo = people["phones"]["Theo"]
    body = theo.client.get("/v1/tv/names", headers=theo.h).json()
    assert set(body["movies"]) == {"8"} and body["series"] == {}
    assert set(body["categories"]["vod"]) == {"|EN| KIDS"}
