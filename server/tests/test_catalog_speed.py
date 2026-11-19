"""Play and search read the folded catalog once per change, not once per call (Nov 19 2026: a film took 10
to 15 s to start on the phone while "finding a free login", and series search timed out at 30 s)."""

from __future__ import annotations

import time

from hub import iptvcanon
from hub.iptvcanon import index, observe, playable
from hub.iptvplay import _canonical, _holding


def _many(n: int) -> list[dict]:
    rows = []
    for i in range(n):
        name = f"Film Number {i} (20{i % 30:02d})"
        rows.append({"provider": "alpha", "kind": "movie", "stream_id": str(i), "name": name})
        rows.append({"provider": "alpha", "kind": "series", "stream_id": str(i), "name": f"Show Number {i}"})
    return rows


def test_the_index_is_read_once_until_the_catalog_changes(hub, monkeypatch):
    observe(hub.db, [{"provider": "alpha", "kind": "movie", "stream_id": "1", "tmdb": "603",
                      "name": "The Matrix (1999)"}])
    reads = []
    real = iptvcanon._state
    monkeypatch.setattr(iptvcanon, "_state", lambda db: reads.append(1) or real(db))
    first = index(hub.db)
    for _ in range(20):
        assert index(hub.db) is first
        assert _holding(hub, "vod:alpha:1")["title"] == "The Matrix"
        assert playable(hub.db, kind="movie", title="The Matrix")["ref"] == "vod:alpha:1"
    assert len(reads) == 1
    # A new fold is seen at once.
    observe(hub.db, [{"provider": "alpha", "kind": "movie", "stream_id": "2", "name": "Dune (2021)"}])
    assert _holding(hub, "vod:alpha:2")["title"] == "Dune"
    found = playable(hub.db, kind="movie", title="Dune")
    assert found and _canonical(hub, found["canonical_id"])["title"] == "Dune"


def test_matching_keeps_its_rules(hub):
    observe(hub.db, [
        {"provider": "alpha", "kind": "movie", "stream_id": "1", "tmdb": "603", "year": 1999,
         "name": "The Matrix (1999)"},
        {"provider": "alpha", "kind": "series", "stream_id": "9", "name": "The Matrix"},
    ])
    assert playable(hub.db, kind="series", title="The Matrix")["ref"] == "series:alpha:9"
    assert playable(hub.db, kind="movie", title="Something else", tmdb="603")["ref"] == "vod:alpha:1"
    assert playable(hub.db, kind="movie", title="The Matrix", year=1999)["ref"] == "vod:alpha:1"
    assert playable(hub.db, kind="movie", title="The Matrix", year=2003) is None
    assert playable(hub.db, kind="movie", title="Nothing like it") is None


def test_many_lookups_on_a_big_catalog_stay_fast(hub):
    observe(hub.db, _many(3000))
    index(hub.db).by_title("movie", "")  # built once, as the first search after a refresh does
    start = time.perf_counter()
    for i in range(0, 3000, 75):  # 40 results, as one search marks
        assert playable(hub.db, kind="series", title=f"Show Number {i}") is not None
        assert _holding(hub, f"vod:alpha:{i}") is not None
    # Before, each of these scanned and re-parsed every title: several seconds for 40 on this size.
    assert time.perf_counter() - start < 1.0
