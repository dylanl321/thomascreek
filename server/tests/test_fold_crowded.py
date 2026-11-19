"""Dated shows filed as films keep their date, and a provider that lists one name many times does not fold
them into one title (Nov 19 2026: 79 "WWE SmackDown" entries were one film, so Play opened any of them)."""

from __future__ import annotations

from hub.iptvcanon import SAME_PROVIDER_MAX, index, observe
from hub.iptvnorm import parse


def test_a_date_keeps_two_episodes_apart_and_shows_on_the_card():
    raw = parse("EN - WWE Raw (26/08/2024)", titles=True)
    assert raw.display == "WWE Raw (26/08/2024)" and raw.variant == "2024-08-26" and raw.year is None
    assert parse("WWE Raw 2025-01-06", titles=True).variant == "2025-01-06"
    assert parse("The Matrix (1999)", titles=True).variant is None


def test_dated_entries_fold_only_with_the_same_date(hub):
    observe(hub.db, [
        {"provider": "strong", "kind": "movie", "stream_id": "1", "name": "EN - WWE Raw (26/08/2024)"},
        {"provider": "strong", "kind": "movie", "stream_id": "2", "name": "EN - WWE Raw (06/01/2025)"},
        {"provider": "thomas-creek", "kind": "movie", "stream_id": "3", "name": "WWE Raw (26/08/2024)"},
    ])
    titles = sorted((item["title"], len(item["sources"])) for item in index(hub.db).items)
    assert titles == [("WWE Raw (06/01/2025)", 1), ("WWE Raw (26/08/2024)", 2)]


def test_a_crowded_name_stays_apart_but_real_copies_still_fold(hub):
    rows = [{"provider": "strong", "kind": "movie", "stream_id": str(n), "name": "EN - WWE SmackDown"}
            for n in range(SAME_PROVIDER_MAX + 2)]
    rows += [{"provider": "strong", "kind": "movie", "stream_id": "900", "name": "Dune (2021) 4K"},
             {"provider": "strong", "kind": "movie", "stream_id": "901", "name": "Dune (2021)"},
             {"provider": "thomas-creek", "kind": "movie", "stream_id": "5", "name": "EN - Dune (2021)"}]
    observe(hub.db, rows)
    items = index(hub.db).items
    smack = [item for item in items if item["title"] == "WWE SmackDown"]
    assert len(smack) == SAME_PROVIDER_MAX + 2 and all(len(item["sources"]) == 1 for item in smack)
    assert len({item["id"] for item in smack}) == len(smack)
    dune = [item for item in items if item["title"] == "Dune"]
    assert len(dune) == 1 and len(dune[0]["sources"]) == 3
    # Folding again (nothing changed) keeps them as they are.
    observe(hub.db, rows)
    assert len([item for item in index(hub.db).items if item["title"] == "WWE SmackDown"]) == SAME_PROVIDER_MAX + 2


def test_usa_and_gb_prefixes_are_regions():
    assert parse("USA | ESPN FHD").key == parse("US: ESPN HD").key == "espn"
    assert parse("GB: Sky One").region == "UK"


def test_accounts_say_what_each_provider_list_holds(hub, people):
    hub.config.iptv.providers = []
    hub.vault.set("iptv.beta.spare.password", "secret-spare")
    hub.db.kv_set("iptv.logins", [
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare",
         "password_secret": "iptv.beta.spare.password"},
        {"provider": "gamma", "login": "x", "server": "http://gamma.test", "username": "x",
         "password_secret": "iptv.gamma.x.password"},  # no password saved
    ])
    hub.tv_catalog.ingest_extra("beta", live=[{"stream_id": 1, "name": "ESPN"}],
                                vod=[{"stream_id": 2, "name": "Dune (2021)"}], series=[])
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/accounts", headers=dylan.h)
    lists = {row["provider"]: row for row in body.json()["lists"]}
    assert lists["beta"]["channels"] == 1 and lists["beta"]["films"] == 1 and lists["beta"]["problem"] is None
    assert lists["gamma"]["problem"].startswith("skipped")
    assert "secret-spare" not in body.text and "beta.test" not in body.text
