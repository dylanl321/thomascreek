"""Names the phone walkthrough found raw: card titles, library rows, labels and section-header channels."""

from __future__ import annotations

import asyncio

import httpx

from hub import iptvaddr, tvnames
from hub.iptvplay import KV_USAGE
from hub.routes.tvguide import _polished
from hub.tvcatalog import _public_movie, _public_series_row


def test_card_titles_read_as_people_say_them():
    assert tvnames.polish("EN - Rolling Loud (2026)") == ("Rolling Loud", 2026)
    assert tvnames.polish("|EN| Dune (2021) 4K") == ("Dune", 2021)
    assert tvnames.polish("EN - Blade Runner 2049") == ("Blade Runner 2049", None)
    assert tvnames.polish("Blade Runner 2049") == ("Blade Runner 2049", None)
    body = {"hero": {"kind": "movie", "title": "EN - Holiday (2023) EXTRA"},
            "rows": [{"id": "x", "title": "Holiday movies",
                      "items": [{"kind": "series", "title": "Bluey", "year": 2018}]}]}
    out = _polished(body)
    assert out["hero"] == {"kind": "movie", "title": "Holiday", "year": 2023}
    assert out["rows"][0]["title"] == "Holiday movies" and out["rows"][0]["items"][0]["title"] == "Bluey"


def test_library_rows_carry_a_readable_title_and_label():
    row = _public_movie({"id": "vod:a:1", "name": "EN - Dune (2021) 4K", "category": "|EN| ACTION MOVIES"})
    assert row["name"] == "EN - Dune (2021) 4K" and row["title"] == "Dune" and row["year"] == 2021
    assert row["category_label"] == "Action Movies"
    plain = _public_series_row({"id": "series:a:2", "name": "Bluey", "year": 2018})
    assert "title" not in plain and plain["year"] == 2018


def test_labels_keep_their_acronyms_and_dividers_are_not_channels():
    assert tvnames.category("Espn+ PPV") == "ESPN+ PPV"
    assert tvnames.category("Sky Sports") == "Sky Sports"
    assert tvnames.is_divider("###### ARABIC ######") and tvnames.is_divider("===== UK =====")
    assert not tvnames.is_divider("CNN") and not tvnames.is_divider("A-Team")


def test_an_address_check_keeps_the_logins_expiry_and_connections(hub):
    hub.config.iptv.providers = []
    hub.vault.set("iptv.alpha.house.password", "pw")
    hub.db.kv_set("iptv.logins", [{"provider": "alpha", "login": "house", "server": "http://alpha.test",
                                   "username": "u", "password_secret": "iptv.alpha.house.password"}])
    answer = {"user_info": {"status": "Active", "exp_date": "1893456000", "max_connections": "3", "active_cons": "1"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=answer))
    hub.tv_catalog._transport = transport
    asyncio.run(iptvaddr.check_all(hub, only_with_backups=False))
    kept = hub.db.kv_get(KV_USAGE)["alpha|house"]
    assert kept["max_connections"] == 3 and kept["active_cons"] == 1 and kept["exp_date"] == "1893456000"


def test_an_older_title_is_not_called_new_this_week():
    from datetime import datetime, timezone

    from hub.tvpicks import _lead
    now = datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()
    assert _lead({"badges": ["New"], "reason": "New this week", "year": 2026, "kind": "movie"}, now) == "New this week"
    assert _lead({"badges": ["New"], "reason": "New this week", "year": 2022, "kind": "series"}, now) \
        == "New episodes this week"
    assert _lead({"badges": ["New"], "reason": "In your library", "year": 2022, "kind": "series"}, now) == "Just added"
    assert _lead({"badges": ["Popular"], "year": 2022}, now) == "Trending now"
