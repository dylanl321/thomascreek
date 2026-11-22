"""Work that froze every route on the live hub (Nov 19 2026, /v1/ping/stalls) now runs off the loop or less often,
and the other providers' list reads say why a list was not read."""

from __future__ import annotations

import asyncio
import json

import httpx

from hub import tvcatalog, tvexplore, tvpicks
from hub.tvapp import TvAppError


# -- other providers' lists --------------------------------------------------------------------------------------
def _fake_api(answers):
    async def api(pid, servers, params, wait_s=45):
        assert wait_s >= 120  # a full film list can take minutes
        got = answers[params["action"]]
        if isinstance(got, Exception):
            raise got
        return servers[0], got
    return api


def _logins(cat, monkeypatch):
    monkeypatch.setattr(cat, "_extra_logins",
                        lambda: {"beta": {"username": "u", "password": "p", "servers": ["https://a"]}})
    monkeypatch.setattr(cat, "fold", lambda doc=None: None)


def test_a_failed_list_keeps_its_last_copy_and_says_why(hub, monkeypatch):
    cat = hub.tv_catalog
    _logins(cat, monkeypatch)
    cat.ingest_extra("beta", live=[{"stream_id": 1, "name": "ESPN"}], vod=[{"stream_id": 7, "name": "Old Film"}],
                     series=[])
    req = httpx.Request("GET", "https://a/player_api.php")
    monkeypatch.setattr(cat, "_api", _fake_api({
        "get_live_streams": httpx.Response(200, json=[{"stream_id": 2, "name": "CNN"}], request=req),
        "get_vod_streams": httpx.ReadTimeout("slow", request=req),
        "get_series": httpx.Response(200, json=[{"series_id": 3, "name": "A Show"}], request=req),
    }))
    result = asyncio.run(cat.refresh_extras_here())
    assert result["beta"].startswith("partly read: films timed out (kept the last copy)")
    doc = cat.extra("beta")
    assert [c["name"] for c in doc["channels"]] == ["CNN"]
    assert [m["name"] for m in doc["movies"]] == ["Old Film"]  # the last copy
    assert [s["name"] for s in doc["series"]] == ["A Show"]
    from hub import iptvplay

    monkeypatch.setattr(iptvplay, "logins", lambda h: [{"provider": "beta"}])
    row = next(r for r in cat.list_status() if r["provider"] == "beta")
    assert row["problem"] == "partly read: films timed out (kept the last copy)"
    cat.__dict__["_extra_result"] = {"beta": "not read: couldn't connect"}
    row = next(r for r in cat.list_status() if r["provider"] == "beta")
    assert row["problem"] == "the last read failed: couldn't connect"


def test_a_refused_login_is_named(hub, monkeypatch):
    cat = hub.tv_catalog
    _logins(cat, monkeypatch)
    req = httpx.Request("GET", "https://a/player_api.php")
    refused = httpx.Response(200, json={"user_info": {"auth": 0}}, request=req)
    monkeypatch.setattr(cat, "_api", _fake_api({a: refused for a in
                                                ("get_live_streams", "get_vod_streams", "get_series")}))
    result = asyncio.run(cat.refresh_extras_here())
    assert result["beta"] == "not read: the provider refused the login"
    assert "https://a" not in json.dumps(result) and "u" not in result["beta"].split()


def test_reasons_are_short_and_carry_no_address():
    req = httpx.Request("GET", "https://secret.example/player_api.php?username=x&password=y")
    assert tvcatalog._read_reason(httpx.ConnectError("boom https://secret.example", request=req)) == "couldn't connect"
    assert tvcatalog._read_reason(httpx.ReadTimeout("t", request=req)) == "timed out"
    assert tvcatalog._read_reason(TvAppError(502, "bad_provider", "The TV provider timed out")) == "timed out"
    assert tvcatalog._read_reason(ValueError("answered 502")) == "answered 502"
    assert tvcatalog._read_reason(ValueError("Expecting value: line 1")) == "couldn't be read"
    try:
        tvcatalog._list_body(httpx.Response(200, text="<html>", request=req))
    except ValueError as exc:
        assert str(exc) == "answered something that isn't a list"


def test_old_results_still_read(hub):
    cat = hub.tv_catalog
    cat.__dict__["_extra_result"] = {"x": "not read"}
    assert cat.list_status()[0]["main"] is True


# -- explore feed --------------------------------------------------------------------------------------------------
def test_the_feed_is_built_in_a_worker_thread(hub, monkeypatch):
    threads = []
    real = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        threads.append(getattr(fn, "__name__", ""))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(tvexplore.asyncio, "to_thread", spy)
    asyncio.run(tvexplore.refresh(hub))
    assert "_build" in threads
    raw = hub.db.kv_get(tvexplore.KV)
    assert isinstance(raw.get("adult"), dict) and isinstance(raw.get("kids"), dict) and "pool" in raw


def test_publish_still_returns_both_feeds(hub):
    out = tvexplore.publish(hub, None, can_request=False)
    assert set(out) == {"adult", "kids"}


def test_season_words_match_the_old_score():
    card = {"title": "A Christmas Story", "overview": "Snow falls on a small town.", "genres": ["Comedy"]}
    for season in tvpicks.SEASONS:
        title = f" {tvpicks._name(card['title'])} "
        story = f" {tvpicks._name(card['overview'])} "
        assert tvpicks._season_words(card, title, story, season) == tvpicks._season_score(card, season)


# -- artwork notes ---------------------------------------------------------------------------------------------------
def test_artwork_notes_are_written_once_not_per_title(hub, monkeypatch):
    art = hub.artwork
    writes = []
    real = art._save_json

    def counted(name, doc):
        writes.append(name)
        real(name, doc)

    monkeypatch.setattr(art, "_save_json", counted)
    for i in range(300):
        art.remember(f"vod:t{i}", title=f"Title {i}", kind="movie")
    assert writes == []  # waiting for the timer
    art.flush()
    assert writes == ["notes.json"]
    saved = json.loads((art.dir / "notes.json").read_text())
    assert saved["vod:t299"]["title"] == "Title 299"
    assert not list(art.dir.glob(".*.tmp"))
    if art._flush_timer is not None:
        art._flush_timer.cancel()


def test_big_sdks_are_imported_before_the_hub_listens(monkeypatch):
    import importlib

    from hub import main

    seen = []
    monkeypatch.setattr(importlib, "import_module", lambda name: seen.append(name))
    main._preload()
    assert "anthropic" in seen


def test_explore_builds_the_clean_title_rows_once_per_folded_index(hub, monkeypatch):
    """Explore read a fresh folded index and rebuilt every clean-title row on each feed build, which starved the
    loop for seconds after a start. The rows are now built once per index, and each caller gets its own copies."""
    from hub import iptvcanon

    built = []

    def build(h):
        built.append(1)
        return [{"kind": "movie", "title": "Dune", "movie_id": "vod:thomas-creek:1"}]

    marker = object()
    monkeypatch.setattr(iptvcanon, "peek", lambda db: marker)
    monkeypatch.setattr(tvexplore, "_build_canonical_rows", build)
    first = tvexplore._canonical_rows(hub)
    first[0]["match_only"] = True
    again = tvexplore._canonical_rows(hub)
    assert len(built) == 1 and "match_only" not in again[0]
    monkeypatch.setattr(iptvcanon, "peek", lambda db: object())  # a new index after the state changed
    tvexplore._canonical_rows(hub)
    assert len(built) == 2


def test_explore_waits_for_the_first_title_index_instead_of_building_a_second(hub, monkeypatch):
    """After a start, a background thread builds the title index; Explore building its own copy beside it doubled
    the parse and starved the loop for 20 s (Nov 19 2026). It waits for that build instead."""
    import threading
    import time as _time

    from hub import iptvcanon

    ready = object()
    monkeypatch.setattr(iptvcanon, "peek", lambda db: None)  # a big house, first build still running
    monkeypatch.setattr(iptvcanon, "index", lambda db: (_ for _ in ()).throw(AssertionError("second build")))
    with iptvcanon._BUILD_LOCK:
        iptvcanon._BUILDING.add(id(hub.db))

    def finish():
        _time.sleep(0.6)
        iptvcanon._TITLED[id(hub.db)] = (hub.db, ready)
        with iptvcanon._BUILD_LOCK:
            iptvcanon._BUILDING.discard(id(hub.db))

    threading.Thread(target=finish, daemon=True).start()
    try:
        assert iptvcanon.settled(hub.db, wait_s=5) is ready
    finally:
        iptvcanon._TITLED.pop(id(hub.db), None)
        with iptvcanon._BUILD_LOCK:
            iptvcanon._BUILDING.discard(id(hub.db))


def test_lineups_and_plays_read_the_last_folded_index_while_a_new_one_builds(hub, monkeypatch):
    """Each fold rewrote the state, and the next lineup or play rebuilt the whole index inline: lineups took 8 to
    10 s from abroad (Nov 19 2026). After a write, `recent` answers from the last index and rebuilds behind it."""
    import threading
    import time as _time

    from hub import iptvcanon

    hub.db.kv_set(iptvcanon.KV, {"items": [{"id": "c1", "kind": "live", "sources": [{"ref": "live:a:1"}]}]})
    iptvcanon._INDEX.pop(id(hub.db), None)
    first = iptvcanon.recent(hub.db)  # nothing built yet: built here
    _time.sleep(0.01)
    hub.db.kv_set(iptvcanon.KV, {"items": [{"id": "c2", "kind": "live", "sources": [{"ref": "live:a:2"}]}]})
    gate = threading.Event()
    real = iptvcanon.Index

    def slow_index(state):
        gate.wait(5)
        return real(state)

    monkeypatch.setattr(iptvcanon, "Index", slow_index)
    started = _time.perf_counter()
    again = iptvcanon.recent(hub.db)
    assert again is first and _time.perf_counter() - started < 0.5  # answered from the last index
    gate.set()
    deadline = _time.monotonic() + 5
    while iptvcanon.recent(hub.db) is first and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert iptvcanon.recent(hub.db) is not first  # the rebuild landed


def test_dvr_cleanup_writes_only_when_it_dropped_something(hub, monkeypatch):
    saved = []
    monkeypatch.setattr(hub.dvr, "_save", lambda rows: saved.append(rows))
    hub.dvr.cleanup()
    assert saved == []


def test_copies_answer_from_the_last_index_while_the_title_matcher_saves(hub, monkeypatch):
    """Nov 21 2026: /v1/tv/copies re-read the several-MB folded titles on the hub's loop after each save by the title
    matcher, and the hub stood still 10 to 14 s about every four minutes while a TV was on."""
    import time as _time

    from hub import iptvcanon

    two = [{"ref": "vod:a:1", "quality": "HD"}, {"ref": "vod:a:2", "quality": "SD"}]
    hub.db.kv_set(iptvcanon.KV, {"items": [{"id": "m1", "kind": "movie", "sources": two}]})
    iptvcanon._INDEX.pop(id(hub.db), None)
    first = iptvcanon.copies(hub.db)
    assert first["groups"] == [{"id": "m1", "refs": ["vod:a:1", "vod:a:2"]}]
    assert iptvcanon.copies(hub.db) is first  # worked out once per index
    _time.sleep(0.01)
    hub.db.kv_set(iptvcanon.KV, {"items": [{"id": "m2", "kind": "movie", "sources": two}]})
    built = []
    real = iptvcanon.Index
    monkeypatch.setattr(iptvcanon, "Index", lambda state: built.append(1) or real(state))
    monkeypatch.setattr(iptvcanon, "REFRESH_EVERY_S", 3600.0)
    started = _time.perf_counter()
    assert iptvcanon.copies(hub.db) is first  # the last index answers; nothing parsed on the caller
    assert built == [] and _time.perf_counter() - started < 0.5


def test_sport_channels_are_matched_once_a_minute(hub, monkeypatch):
    from hub import tvsport
    from hub.routes import tvguide

    calls = []
    monkeypatch.setattr(tvsport, "match", lambda games, cat, basic, **kw: calls.append(1) or [{"id": "g"}])
    doc = {"channels": []}
    monkeypatch.setattr(hub.tv_catalog, "load", lambda: doc)
    monkeypatch.setattr(hub.tv_catalog, "basic", lambda home: {"channels": []})
    games = [{"id": "g", "start": 1.0}]
    assert tvguide._sport_channels(hub, games, None, False, 600.0) == [{"id": "g"}]
    assert tvguide._sport_channels(hub, games, None, False, 630.0) == [{"id": "g"}]
    assert len(calls) == 1
    tvguide._sport_channels(hub, games, None, False, 700.0)  # the next minute
    assert len(calls) == 2


# -- warm before serving (review P9, Nov 21 2026) ----------------------------------------------------------------
def test_the_warm_builds_names_and_copies_before_the_first_call(hub, monkeypatch):
    from hub import iptvcanon, warm
    from hub.routes import tvguide

    hub.tv_catalog.path.parent.mkdir(parents=True, exist_ok=True)
    hub.tv_catalog.path.write_text(json.dumps({"vod": [{"stream_id": 1, "name": "EN - Up (2009)"}]}))
    built = []
    real = tvguide.tvnames.names
    monkeypatch.setattr(tvguide.tvnames, "names", lambda *a, **k: built.append(k.get("kids_only")) or real(*a, **k))
    asyncio.run(warm.before_serving(hub))
    assert sorted(built) == [False, True]
    assert hub.__dict__["_tv_names"][False][0] is hub.tv_catalog.load()
    assert id(hub.db) in iptvcanon._COPIES
    # A later names call answers from what the warm built.
    tvguide.names_reply(hub, False)
    assert len(built) == 2


def test_a_slow_warm_does_not_hold_the_start(hub, monkeypatch):
    import time as _time

    from hub import warm

    monkeypatch.setattr(warm, "_from_disk", lambda h: _time.sleep(0.5))

    async def run():
        began = _time.monotonic()
        await warm.before_serving(hub, limit=0.05)
        return _time.monotonic() - began

    assert asyncio.run(run()) < 0.4


def test_checking_other_lists_age_never_parses_them_on_the_loop(hub, monkeypatch):
    """`ensure` asks whether the other providers' lists are due on the hub's loop; reading `fetched_at` parsed each
    whole list there after every refresh and held every caller 1.1 s (Nov 21 2026)."""
    cat = hub.tv_catalog
    _logins(cat, monkeypatch)
    cat.ingest_extra("beta", live=[{"stream_id": 1, "name": "ESPN"}], vod=[], series=[])
    cat.__dict__.pop("_extra_cache", None)
    parsed = []
    monkeypatch.setattr(cat, "extra", lambda pid: parsed.append(pid) or {})
    import time

    cat._extras_due(time.time())
    assert parsed == [] and not cat.__dict__.get("_extras_running")  # fresh: nothing read, nothing started
    assert cat._extra_written("beta") > 0 and cat._extra_written("gone") == 0.0


def test_a_finished_refresh_rebuilds_the_answers_made_from_it(hub, monkeypatch):
    """After a refresh the names, lineup and sport answers are built again behind it, before a TV asks."""
    cat = hub.tv_catalog
    called = []
    monkeypatch.setattr(cat, "refresh_here", lambda: asyncio.sleep(0, result={"ok": 1}))
    cat.on_refreshed = lambda: called.append(1)
    assert asyncio.run(cat.refresh()) == {"ok": 1}
    assert called == [1]


def test_habits_are_learned_in_a_worker_thread(hub, monkeypatch):
    import threading

    smart = hub.smart
    seen = []
    monkeypatch.setattr(smart.weather, "refresh", lambda: asyncio.sleep(0))
    monkeypatch.setattr(hub.forecasts, "warm_home", lambda: asyncio.sleep(0))
    monkeypatch.setattr(smart, "learn_habits", lambda: seen.append(threading.current_thread().name))
    smart._habits_at = 0
    loop_thread = threading.current_thread().name
    asyncio.run(smart.refresh())
    assert seen and seen[0] != loop_thread


def test_saved_pages_are_read_a_few_hundred_at_a_time(hub, monkeypatch):
    from hub import db as dbmod

    monkeypatch.setattr(dbmod, "DETAILS_PAGE", 3)
    rows = [("episodes", str(n), json.dumps({"n": n})) for n in range(8)]
    hub.db.details_put(rows)
    reads = []
    real = hub.db.all
    monkeypatch.setattr(hub.db, "all", lambda sql, args=(): reads.append(sql) or real(sql, args))
    got = hub.db.details_rows()
    assert sorted(got) == sorted(rows)
    assert len(reads) == 3


def test_a_background_index_rebuild_works_out_the_copies_too(hub, monkeypatch):
    """Nov 21 2026: the first /v1/tv/copies after a rebuild worked the copies out itself and took 3 s live."""
    from hub import iptvcanon

    two = [{"ref": "vod:a:1", "quality": "HD"}, {"ref": "vod:a:2", "quality": "SD"}]
    hub.db.kv_set(iptvcanon.KV, {"items": [{"id": "m1", "kind": "movie", "sources": two}]})
    iptvcanon._INDEX.pop(id(hub.db), None)
    before = iptvcanon.tally()["n"]
    iptvcanon._refresh(hub.db)
    assert iptvcanon.tally()["n"] == before + 1
    memo = iptvcanon._COPIES[id(hub.db)]
    assert memo[0] is iptvcanon._INDEX[id(hub.db)][1]
    worked = []
    monkeypatch.setattr(iptvcanon, "_copies", lambda found: worked.append(1) or {})
    assert iptvcanon.copies(hub.db)["groups"] == [{"id": "m1", "refs": ["vod:a:1", "vod:a:2"]}]
    assert worked == []


def test_the_timings_page_counts_catalog_index_builds(hub):
    from starlette.testclient import TestClient

    from hub.main import build_app

    home = TestClient(build_app(hub, start_background=False), base_url="https://hub.test",
                      client=("192.168.1.50", 40000))
    body = home.get("/v1/ping/timings").json()
    assert set(body["canon"]) >= {"n", "max_ms", "last_ms", "last_at"}
    assert set(body["canon"]["catalog"]) >= {"fetched_at", "guide_at", "guide_channels", "reads", "extras_tried"}
    assert {"builds", "pid", "writer"} <= set(body["canon"])


def test_each_index_build_says_why_and_the_state_names_its_writer(hub):
    """Nov 22 2026: the folded catalog was parsed again every 30 min with no provider read. Each build now says why
    it ran and when the state it read was written, and each write leaves which process and step wrote it."""
    import os

    from hub import iptvcanon

    iptvcanon._put_state(hub.db, {"items": [{"id": "a", "kind": "movie", "title": "Up", "sources": []}]})
    writer = hub.db.kv_get(iptvcanon.KV_WRITER)
    assert writer["pid"] == os.getpid() and writer["step"].startswith("test_each_index_build")
    iptvcanon._INDEX.pop(id(hub.db), None)
    iptvcanon.index(hub.db)
    last = iptvcanon.tally(hub.db)["builds"][-1]
    assert last["why"] == "first" and last["written_at"] >= writer["at"] - 1


def test_a_provider_that_never_reads_is_tried_again_once_an_hour_not_on_every_call(hub, monkeypatch):
    """Nov 21 2026: a provider whose address didn't resolve never got a list file, so every call found it due and the
    hub read and folded every provider again back to back, rebuilding the folded catalog every 30 min."""
    from hub import tvcatalog

    cat = hub.tv_catalog
    monkeypatch.setattr(cat, "_extra_logins", lambda: {"eagle": {"servers": ["https://a"]}})
    reads = []

    async def read():
        reads.append(1)
        return {}

    monkeypatch.setattr(cat, "refresh_extras", read)

    async def calls(times):
        for t in times:
            cat._extras_due(t)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(calls([1_000_000.0, 1_000_060.0, 1_000_000.0 + tvcatalog.EXTRA_RETRY_S - 1]))
    assert reads == [1]
    asyncio.run(calls([1_000_000.0 + tvcatalog.EXTRA_RETRY_S]))
    assert reads == [1, 1]


def test_folding_the_same_lists_again_does_not_rewrite_the_state(hub):
    """Nov 22 2026: a provider read that changed nothing (the hourly retry of a provider that can't be reached)
    still rewrote the folded catalog from the provider-read child, and the hub parsed all of it again (12 to 14 s)."""
    from hub import iptvcanon

    def doc(*names):
        movies = [{"id": f"vod:{i}", "name": n} for i, n in enumerate(names, 1)]
        return iptvcanon.sources_from_catalog({"movies": movies})

    sources = doc("EN - Up (2009)")
    iptvcanon.observe(hub.db, sources)
    first = hub.db.kv_stamp(iptvcanon.KV)
    iptvcanon.observe(hub.db, sources)
    assert hub.db.kv_stamp(iptvcanon.KV) == first
    assert first is not None
    iptvcanon.observe(hub.db, doc("EN - Up (2009)", "EN - Cars (2006)"))
    assert hub.db.kv_stamp(iptvcanon.KV) != first
