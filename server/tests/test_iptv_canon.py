"""Messy provider names fold into one title only when the hub is sure."""

from __future__ import annotations

import hashlib
import json

from hub.iptvcanon import merge, observe, playable, search, split, unsure

MOVIES = [
    {"provider": "alpha", "kind": "movie", "stream_id": "100", "tmdb": "603",
     "name": "US| The Matrix (1999) [4K] HEVC"},
    {"provider": "beta", "kind": "movie", "stream_id": "55", "tmdb": "603",
     "name": "EN - The Matrix 1999 FHD"},
    {"provider": "alpha", "kind": "movie", "stream_id": "101", "tmdb": "604",
     "name": "The Matrix Reloaded (2003) HD"},
    {"provider": "alpha", "kind": "live", "stream_id": "10", "tvg_id": "espn.us",
     "name": "ESPN HD", "logo": "https://img.example/espn.png"},
    {"provider": "beta", "kind": "live", "stream_id": "11", "tvg_id": "espn.us",
     "name": "ESPN FHD", "logo": "https://img.example/espn.png"},
    {"provider": "alpha", "kind": "live", "stream_id": "12", "name": "ESPN 2 HD"},
    {"provider": "alpha", "kind": "live", "stream_id": "13", "name": "ESPN Deportes"},
    {"provider": "alpha", "kind": "live", "stream_id": "14", "name": "CNN East"},
    {"provider": "alpha", "kind": "live", "stream_id": "15", "name": "CNN West"},
    {"provider": "alpha", "kind": "live", "stream_id": "16", "name": "CNN +1"},
    {"provider": "alpha", "kind": "movie", "stream_id": "200", "name": "ᴿᴬᵂ Dune (2021)"},
    {"provider": "beta", "kind": "movie", "stream_id": "201", "name": "Dune 2021 HD", "tmdb": "438631"},
]


def _id(identity: str) -> str:
    return "can_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def test_the_catalog_route_matches_the_contract(hub, people):
    from tests.test_iptv_canon import MOVIES
    observe(hub.db, MOVIES)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/catalog", headers=dylan.h, params={"q": "matrix", "kind": "movie"}).json()
    assert body["items"][0]["title"] == "The Matrix"
    assert body["items"][0]["sources"][0]["quality"] == "2160p"
    assert "password" not in dylan.client.get("/v1/tv/catalog", headers=dylan.h).text
    missing = dylan.client.post("/v1/tv/catalog/merge", headers=dylan.h, json={"ids": ["can_missing", "can_other"]})
    assert missing.status_code == 404 and missing.json()["message"] == "That title isn't in the catalog."
    avery = people["phones"]["Avery"]
    assert avery.client.post("/v1/tv/catalog/merge", headers=avery.h, json={"ids": ["can_a", "can_b"]}).status_code == 403


def test_messy_names_fold_only_when_sure(hub):
    db = hub.db
    observe(db, MOVIES)
    matrix = search(db, "matrix", "movie")["items"]
    assert matrix == [{
        "id": _id("movie|603"),
        "kind": "movie",
        "title": "The Matrix",
        "year": 1999,
        "sources": [
            {"provider": "alpha", "stream_id": "100", "ref": "vod:alpha:100", "quality": "2160p",
             "language": None, "name": "US| The Matrix (1999) [4K] HEVC"},
            {"provider": "beta", "stream_id": "55", "ref": "vod:beta:55", "quality": "1080p",
             "language": "en", "name": "EN - The Matrix 1999 FHD"},
        ],
    }, {
        "id": _id("movie|604"),
        "kind": "movie",
        "title": "The Matrix Reloaded",
        "year": 2003,
        "sources": [
            {"provider": "alpha", "stream_id": "101", "ref": "vod:alpha:101", "quality": "720p",
             "language": None, "name": "The Matrix Reloaded (2003) HD"},
        ],
    }]
    live = {item["title"] + "|" + str(item.get("variant")) + "|" + str(item.get("timeshift")): item
            for item in search(db, "", "live")["items"]}
    espn = live["ESPN|None|0"]
    assert [source["stream_id"] for source in espn["sources"]] == ["10", "11"]
    assert "ESPN|2|0" in live and "ESPN|deportes|0" in live
    assert "CNN|east|0" in live and "CNN|west|0" in live and "CNN|None|1" in live
    assert playable(db, kind="movie", title="The Matrix", year=1999, tmdb="603")["ref"] == "vod:alpha:100"


def test_unsure_pairs_stay_apart_until_someone_says(hub):
    db = hub.db
    sources = [
        {"provider": "alpha", "kind": "movie", "stream_id": "1", "name": "Dune", "year": 2021},
        {"provider": "beta", "kind": "movie", "stream_id": "2", "name": "Dune"},
    ]
    calls: list = []
    observe(db, sources)
    assert len(search(db, "dune")["items"]) == 2
    assert calls == []
    from hub.iptvcanon import _pair_id, _source, ai_payload
    left, right = _source(sources[0]), _source(sources[1])
    assert left and right
    pair_id = _pair_id(left, right)
    sent = ai_payload([{"id": pair_id, "kind": "movie", "left": "Dune", "right": "Dune", "left_year": 2021,
                        "right_year": None, "logo": "https://user:pass@cdn.example/a.png"}])
    assert "pass" not in json.dumps(sent) and "cdn.example" not in json.dumps(sent)
    observe(db, [
        {**sources[0], "name": "Dune (2021)"},
        sources[1],
    ], decisions={pair_id: True})
    assert len(search(db, "dune")["items"]) == 1
    observe(db, [
        {**sources[0], "name": "Dune (2021) HD"},
        sources[1],
    ], decisions={pair_id: False})
    assert len(search(db, "dune")["items"]) == 1 and calls == []


def test_an_admin_merge_wins_and_a_split_is_remembered(hub):
    db = hub.db
    observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "name": "CNN"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "name": "Cable News Network"},
    ])
    rows = search(db, "", "live")["items"]
    assert len(rows) == 2
    merged = merge(db, [rows[0]["id"], rows[1]["id"]])
    assert merged["ok"] is True
    assert len(search(db, "", "live")["items"]) == 1
    again = observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "name": "CNN HD"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "name": "Cable News Network"},
    ])
    assert len(again["items"]) == 1
    parts = split(db, merged["id"], "beta", "2")
    assert parts["ok"] is True and parts["id"] != merged["id"]
    assert len(search(db, "", "live")["items"]) == 2
    observe(db, [
        {"provider": "alpha", "kind": "live", "stream_id": "1", "tvg_id": "cnn.us", "name": "CNN"},
        {"provider": "beta", "kind": "live", "stream_id": "2", "tvg_id": "cnn.us", "name": "CNN"},
    ])
    assert len(search(db, "", "live")["items"]) == 2
    assert unsure(db)["unsure"] == [] or isinstance(unsure(db)["unsure"], list)


def test_posters_do_not_split_a_film_and_tmdb_ids_fold_it(hub):
    db = hub.db
    observe(db, [
        {"provider": "p", "kind": "movie", "stream_id": "1", "name": "Gladiator (2000)", "logo": "https://a.example/x.jpg"},
        {"provider": "p", "kind": "movie", "stream_id": "2", "name": "Gladiator 2000 4K", "logo": "https://a.example/y.jpg"},
        {"provider": "p", "kind": "movie", "stream_id": "3", "name": "Heist", "tmdb": "1234"},
        {"provider": "p", "kind": "movie", "stream_id": "4", "name": "Heist (2015)", "tmdb": "1234"},
    ])
    assert len(search(db, "gladiator")["items"]) == 1
    assert len(search(db, "heist")["items"]) == 1
    assert unsure(db)["unsure"] == []


def test_provider_tmdb_ids_reach_the_fold(hub):
    doc = hub.tv_catalog.ingest(vod=[
        {"stream_id": 1, "name": "Knives Out", "tmdb": "546554"},
        {"stream_id": 2, "name": "Knives Out", "tmdb": "546554"},
        {"stream_id": 3, "name": "Star Wars", "tmdb": "0"},
    ], vod_categories=[], live=[], live_categories=[], series=[], series_categories=[])
    assert doc["movies"][0]["tmdb"] == "546554" and "tmdb" not in doc["movies"][2]
    assert len(search(hub.db, "knives", "movie")["items"]) == 1


def test_waiting_pairs_name_real_titles_and_can_be_answered(hub):
    from hub.iptvcanon import keep_apart
    db = hub.db
    observe(db, [
        {"provider": "p", "kind": "movie", "stream_id": "1", "name": "Star Wars", "year": 1977},
        {"provider": "p", "kind": "movie", "stream_id": "2", "name": "Star Wars"},
        {"provider": "p", "kind": "movie", "stream_id": "3", "name": "The Whale", "year": 2022},
        {"provider": "p", "kind": "movie", "stream_id": "4", "name": "The Whale"},
    ])
    rows = unsure(db)["unsure"]
    assert len(rows) == 2
    ids = {item["id"] for item in search(db)["items"]}
    assert all(row["left"] in ids and row["right"] in ids for row in rows)
    whale = next(row for row in rows if row["title"] == "The Whale")
    stars = next(row for row in rows if row["title"] == "Star Wars")
    assert merge(db, [whale["left"], whale["right"]])["ok"]
    assert [row["id"] for row in unsure(db)["unsure"]] == [stars["id"]]
    assert keep_apart(db, stars["id"])
    assert unsure(db)["unsure"] == []
    assert len(search(db, "star wars")["items"]) == 2


async def test_the_model_sorts_waiting_pairs_with_more_than_the_title(hub):
    from hub.iptvcanon import settle
    db = hub.db
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 1, "name": "Heist (2001)", "category_id": "1", "genre": "Crime"},
        {"stream_id": 2, "name": "Heist", "category_id": "2", "stream_icon": "https://u:p@cdn.example/h.jpg"},
        {"stream_id": 3, "name": "Seven Samurai", "year": "1954", "category_id": "1"},
        {"stream_id": 4, "name": "Seven Samurai", "category_id": "2"},
        {"stream_id": 5, "name": "Dancer In The Dark", "year": "2000"},
        {"stream_id": 6, "name": "Dancer in the Dark"},
    ], vod_categories=[{"category_id": "1", "category_name": "Movies"}, {"category_id": "2", "category_name": "4K"}],
        live=[], live_categories=[], series=[], series_categories=[])
    assert unsure(db)["total"] == 3
    seen: list = []

    async def ask(_hub, pairs):
        seen.extend(pairs)
        out = {}
        for pair in pairs:
            if pair["left"] == "Seven Samurai":
                out[pair["id"]] = True
            elif pair["left"] == "Heist":
                out[pair["id"]] = False
        return out

    result = await settle(hub, ask=ask)
    assert result == {"folded": 1, "apart": 1, "left": 1}
    heist = next(pair for pair in seen if pair["left"] == "Heist")
    payload = json.dumps(ai_payload_of(heist))
    assert "Crime" in payload and "4K" in payload and "cdn.example" not in payload
    assert len(search(db, "samurai")["items"]) == 1 and len(search(db, "heist")["items"]) == 2
    # Asked once: the undecided pair isn't sent again for a week.
    seen.clear()
    assert (await settle(hub, ask=ask))["left"] == 1 and seen == []


def ai_payload_of(pair):
    from hub.iptvcanon import ai_payload
    return ai_payload([pair])


def test_a_wrong_shared_tmdb_id_never_pours_another_film_into_a_title(hub):
    rows = [
        {"provider": "tc", "kind": "movie", "stream_id": "1", "tmdb": "155", "name": "The Dark Knight (2008)"},
        {"provider": "tc", "kind": "movie", "stream_id": "2", "tmdb": "155", "name": "EN - The Dark Knight 4K"},
        {"provider": "tc", "kind": "movie", "stream_id": "3", "tmdb": "155", "name": "Some Other Film (2015)"},
    ]
    items = {item["title"]: item for item in observe(hub.db, rows)["items"]}
    assert sorted(s["stream_id"] for s in items["The Dark Knight"]["sources"]) == ["1", "2"]
    assert [s["stream_id"] for s in items["Some Other Film"]["sources"]] == ["3"]
    assert items["The Dark Knight"]["id"] == _id("movie|155")
    assert playable(hub.db, kind="movie", title="The Dark Knight", tmdb="155")["canonical_id"] == _id("movie|155")


def test_a_streaming_service_tag_is_not_part_of_the_title(hub):
    rows = [
        {"provider": "tc", "kind": "series", "stream_id": "1", "name": "NF - KAOS"},
        {"provider": "tc", "kind": "series", "stream_id": "2", "name": "KAOS"},
        {"provider": "tc", "kind": "movie", "stream_id": "3", "name": "D+ - Moana 2 (2024)"},
        {"provider": "tc", "kind": "movie", "stream_id": "4", "name": "EN| Moana 2 2024 4K"},
        {"provider": "tc", "kind": "live", "stream_id": "5", "name": "HBO - East"},
        {"provider": "tc", "kind": "live", "stream_id": "6", "name": "East"},
    ]
    items = observe(hub.db, rows)["items"]
    by_title = {item["title"]: item for item in items if item["kind"] != "live"}
    assert sorted(s["stream_id"] for s in by_title["KAOS"]["sources"]) == ["1", "2"]
    assert sorted(s["stream_id"] for s in by_title["Moana 2"]["sources"]) == ["3", "4"]  # the 2 is the title's
    live = [item for item in items if item["kind"] == "live"]
    assert len(live) == 2
    assert playable(hub.db, kind="series", title="NF - KAOS") is not None


async def test_a_batch_of_model_answers_writes_the_state_once(hub, monkeypatch):
    """Each answer used to read and write the whole several-MB state on the event loop (Nov 19 2026)."""
    from hub import iptvcanon
    from hub.iptvcanon import KV, settle
    db = hub.db
    rows = []
    for i in range(6):
        rows += [{"stream_id": 10 * i + 1, "name": f"Film {i} ({2000 + i})"},
                 {"stream_id": 10 * i + 2, "name": f"Film {i}"}]
    hub.tv_catalog.ingest(vod=rows, vod_categories=[], live=[], live_categories=[], series=[], series_categories=[])
    assert unsure(db)["total"] == 6
    writes = []
    real = db.kv_set_text
    monkeypatch.setattr(db, "kv_set_text", lambda key, text: (writes.append(key), real(key, text))[1])

    async def ask(_hub, pairs):
        return {pair["id"]: i % 2 == 0 for i, pair in enumerate(pairs)}

    result = await settle(hub, ask=ask)
    assert result == {"folded": 3, "apart": 3, "left": 0}
    assert writes.count(KV) == 1
    assert iptvcanon.unsure(db)["total"] == 0


async def test_the_matcher_reads_the_saved_state_once_a_batch_not_once_a_pair(hub, monkeypatch):
    """The live state is several MB; each parse held the interpreter lock 3 to 4 s and the matcher parsed it once
    per waiting pair (Nov 20 2026)."""
    from hub import iptvcanon
    hub.tv_catalog.ingest(vod=[
        {"stream_id": 1, "name": "Heist (2001)", "category_id": "1"},
        {"stream_id": 2, "name": "Heist", "category_id": "2"},
        {"stream_id": 3, "name": "Seven Samurai", "year": "1954"}, {"stream_id": 4, "name": "Seven Samurai"},
        {"stream_id": 5, "name": "Dancer In The Dark", "year": "2000"}, {"stream_id": 6, "name": "Dancer in the Dark"},
    ], vod_categories=[{"category_id": "1", "category_name": "Movies"}, {"category_id": "2", "category_name": "4K"}],
        live=[], live_categories=[], series=[], series_categories=[])
    iptvcanon.index(hub.db)
    parses = []
    real = iptvcanon._state
    monkeypatch.setattr(iptvcanon, "_state", lambda db: parses.append(1) or real(db))
    seen: list = []

    async def ask(_hub, pairs):
        seen.extend(pairs)
        return {}

    await iptvcanon.settle(hub, ask=ask, force=True)
    assert len(seen) == 3 and all(pair.get("left_info") for pair in seen)
    assert parses == []


def test_a_title_is_matched_under_the_name_its_card_shows():
    """Nov 2026, on the house's lists: the matcher read a number as a channel variant and "US" as a place, so
    "District 9" was matched as "District" and "One of Us Is Lying" as "One of Is Lying"."""
    from hub import tvnames
    from hub.iptvnorm import parse

    for raw, name, key, year in (
        ("NF - District 9 (2009)", "District 9", "district 9", 2009),
        ("NF - Force 2", "Force 2", "force 2", None),
        ("EN - One of Us Is Lying (2021)", "One of Us Is Lying", "one of us is lying", 2021),
        ("EN - West Side Story (1961)", "West Side Story", "west side story", 1961),
        ("EN - Munich (2005) A DIRECTOR, A NOTE", "Munich", "munich", 2005),
        ("EN - Carême (2025)", "Carême", "careme", 2025),
        ("NF - 1941 (1979)", "1941", "1941", 1979),
        ("[US] Hot Rod - 2007", "Hot Rod", "hot rod", 2007),
    ):
        got = parse(raw, titles=True)
        assert (got.display, got.key, got.year, got.variant) == (name, key, year, None), raw
        assert tvnames.polish(raw)[0] == name, raw  # the card shows the same name
        assert parse(got.display, got.year, titles=True).key == key, raw  # a saved title reads back the same
    # A bare number on the end stays on the card and is the year for matching: it meets "First Day Out (2023)".
    bare, bracketed = parse("[US] First Day Out 2023", titles=True), parse("EN - First Day Out (2023)", titles=True)
    assert bare.display == "First Day Out 2023" and (bare.key, bare.year) == (bracketed.key, bracketed.year)
    assert parse("EN - Blade Runner 2049", titles=True).year == 2049  # never folds with the 1982 film by name
    # A dated show keeps its date as the variant, as before.
    raw_show = parse("WWE Raw (26/08/2024)", titles=True)
    assert raw_show.variant == "2024-08-26" and raw_show.key == "wwe raw"
    # Channels keep their own rules: ESPN 2 is ESPN's second feed, East is a feed.
    assert parse("US| ESPN 2 HD").variant == "2" and parse("HBO East").variant == "east"


def test_sequels_fold_with_their_own_copies_and_not_with_each_other(hub):
    rows = [
        {"provider": "tc", "kind": "movie", "stream_id": "1", "name": "EN - The Equalizer (2014)"},
        {"provider": "tc", "kind": "movie", "stream_id": "2", "name": "EN - The Equalizer 2 (2018)"},
        {"provider": "beta", "kind": "movie", "stream_id": "3", "name": "NF - The Equalizer 2 (2018) 4K"},
        {"provider": "beta", "kind": "movie", "stream_id": "4", "name": "EN - One of Us Is Lying (2021)"},
        {"provider": "tc", "kind": "movie", "stream_id": "5", "name": "EN - One of Is Lying (2021)"},
    ]
    items = observe(hub.db, rows)["items"]
    by_title = {item["title"]: sorted(s["stream_id"] for s in item["sources"]) for item in items}
    assert by_title["The Equalizer"] == ["1"] and by_title["The Equalizer 2"] == ["2", "3"]
    assert by_title["One of Us Is Lying"] == ["4"] and by_title["One of Is Lying"] == ["5"]
