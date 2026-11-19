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
    # A new fold is seen as soon as the index rebuilt behind the call (iptvcanon.recent): a play never waits for
    # that rebuild (Nov 19 2026: lineups and plays rebuilt it inline after every fold, 8 to 10 s from abroad).
    observe(hub.db, [{"provider": "alpha", "kind": "movie", "stream_id": "2", "name": "Dune (2021)"}])
    deadline = time.monotonic() + 5
    while _holding(hub, "vod:alpha:2") is None and time.monotonic() < deadline:
        time.sleep(0.02)
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


def test_search_never_waits_for_a_big_catalog_to_be_indexed(hub, monkeypatch):
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "The Office"}])
    monkeypatch.setattr(iptvcanon, "SMALL_STATE", 0)  # treat this catalog as the big live one
    iptvcanon._INDEX.pop(id(hub.db), None)
    started = []
    monkeypatch.setattr(iptvcanon.threading, "Thread", lambda target, args, **_: type(
        "T", (), {"start": lambda self: started.append(target(*args))})())
    rows = [{"id": "series:thomas-creek:9", "name": "The Office"}]
    from hub.tvcatalog import _mark_canonical

    _mark_canonical(hub.db, rows, "series")
    assert started  # the index was built off the request
    _mark_canonical(hub.db, rows, "series")
    assert rows[0]["canonical_id"] == _holding(hub, "series:thomas-creek:9")["id"]


def test_search_never_parses_titles_of_an_index_a_play_built(hub, monkeypatch):
    """A play or copies lookup builds the index without its title map; search must not then parse every title
    itself (2.2 to 2.5 s per search on the live hub, Nov 19 2026). It uses the last index with titles instead."""
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "The Office"}])
    monkeypatch.setattr(iptvcanon, "SMALL_STATE", 0)
    iptvcanon._INDEX.pop(id(hub.db), None)
    iptvcanon._TITLED.pop(id(hub.db), None)
    started = []
    monkeypatch.setattr(iptvcanon.threading, "Thread", lambda target, args, **_: type(
        "T", (), {"start": lambda self: started.append(args)})())  # the background build never runs here
    plain = index(hub.db)  # as a play would
    assert plain._titles is None
    assert iptvcanon.peek(hub.db) is None  # nothing titled yet: rows go without canonical_id
    assert plain._titles is None  # and the search did not parse the titles itself
    assert started


def test_a_big_house_skips_the_state_size_scan(hub, monkeypatch):
    """Once a big titled index exists, a changed state rebuilds in the background without `length(value)`, which
    reads the whole several-MB state (0.6 to 0.9 s per search on the live hub, Nov 19 2026)."""
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "The Office"}])
    iptvcanon._INDEX.pop(id(hub.db), None)
    iptvcanon._TITLED.pop(id(hub.db), None)
    first = iptvcanon.peek(hub.db)  # small: built on the spot
    assert first is not None and first._titles is not None
    monkeypatch.setattr(iptvcanon, "SMALL_ITEMS", 1)
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "The Office"},
                     {"provider": "thomas-creek", "kind": "movie", "stream_id": "3", "name": "Up (2009)"}])
    monkeypatch.setattr(iptvcanon, "_size", lambda db: (_ for _ in ()).throw(AssertionError("size scanned")))
    started = []
    monkeypatch.setattr(iptvcanon.threading, "Thread", lambda target, args, **_: type(
        "T", (), {"start": lambda self: started.append(args)})())
    assert iptvcanon.peek(hub.db) is first  # the last titled index, while the new one builds
    assert started


def test_the_title_map_is_rebuilt_at_most_every_few_minutes(hub, monkeypatch):
    """The state is written on each fold and model batch; rebuilding after each write held searches ~1 s."""
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "The Office"}])
    iptvcanon._INDEX.pop(id(hub.db), None)
    iptvcanon._TITLED.pop(id(hub.db), None)
    monkeypatch.setattr(iptvcanon, "SMALL_STATE", 0)
    monkeypatch.setattr(iptvcanon.threading, "Thread", lambda target, args, **_: type(
        "T", (), {"start": lambda self: target(*args)})())
    first = iptvcanon.peek(hub.db) or iptvcanon.peek(hub.db)  # built in the "background" on the first call
    assert first is not None
    observe(hub.db, [{"provider": "thomas-creek", "kind": "movie", "stream_id": "3", "name": "Up (2009)"}])
    assert iptvcanon.peek(hub.db) is first  # just built: the state change waits
    monkeypatch.setitem(iptvcanon._TITLED_AT, id(hub.db), iptvcanon._TITLED_AT[id(hub.db)] - 301)
    iptvcanon.peek(hub.db)  # a few minutes on, it is rebuilt (here at once; on the hub in the background)
    assert iptvcanon.peek(hub.db) is not first
