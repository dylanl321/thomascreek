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
