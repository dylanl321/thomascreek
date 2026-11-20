"""Big JSON documents are read an element at a time, so the event loop keeps running (Nov 20 2026)."""

from __future__ import annotations

import json
import time

import pytest

from hub import gentlejson, iptvcanon


@pytest.mark.parametrize("doc", [
    {}, {"a": []}, {"a": {}}, {"a": 1, "b": "x", "c": None, "d": True, "e": 1.5},
    {"items": [{"id": "m:1", "sources": [{"ref": "vod:a:1", "name": "Ä \"quoted\" \\ é"}]}, [], {}, 3, "s"],
     "by_source": {"a|movie|1": "m:1", "": ""}, "nested": {"k": {"deep": [1, [2, [3]]]}}, "z": [None, False]},
    [1, 2, 3], "text", 7,
])
@pytest.mark.parametrize("indent", [None, 2])
def test_the_gentle_reader_gives_what_json_gives(doc, indent):
    text = json.dumps(doc, indent=indent, ensure_ascii=False)
    assert gentlejson.loads(text) == json.loads(text)


@pytest.mark.parametrize("bad", ["", "{", '{"a": [1, 2}', '{"a": 1} x', '{"a" 1}', "{'a': 1}", '{"a": [1,]}'])
def test_bad_text_raises_like_json(bad):
    with pytest.raises(json.JSONDecodeError):
        gentlejson.loads(bad)


def test_a_big_state_is_read_in_small_steps(monkeypatch):
    """No single C call reads more than one element below the top level."""
    doc = {"items": [{"id": f"m:{i}", "sources": [{"ref": f"vod:a:{i}"}]} for i in range(2000)],
           "by_source": {f"a|movie|{i}": f"m:{i}" for i in range(2000)}}
    text = json.dumps(doc)
    longest = 0
    real = gentlejson._DEC.raw_decode

    class Counting:
        def raw_decode(self, s, i):
            nonlocal longest
            value, end = real(s, i)
            longest = max(longest, end - i)
            return value, end

    monkeypatch.setattr(gentlejson, "_DEC", Counting())
    assert gentlejson.loads(text) == doc
    assert longest < 100


def test_the_folded_index_is_rebuilt_at_most_every_few_minutes(hub, monkeypatch):
    monkeypatch.setattr(iptvcanon, "REFRESH_EVERY_S", 300.0)
    db = hub.db
    db.kv_set(iptvcanon.KV, {"items": [{"id": "movie:a", "kind": "movie", "title": "A", "sources": []}]})
    iptvcanon._INDEX.pop(id(db), None)
    first = iptvcanon.recent(db)
    time.sleep(0.01)
    db.kv_set(iptvcanon.KV, {"items": [{"id": "movie:b", "kind": "movie", "title": "B", "sources": []}]})
    assert iptvcanon.recent(db) is first  # written again moments later: the last index keeps answering
    assert "movie:b" in iptvcanon.index(db).by_id  # a caller that must see the write still does


def test_the_gentle_writer_gives_the_same_text_as_json_dumps():
    docs = [
        {},
        {"items": [], "by_source": {}},
        {"items": [{"id": f"can_{n}", "title": f"Tiçle \"{n}\"", "sources": [{"provider": "a", "stream_id": n}]}
                   for n in range(700)],
         "by_source": {f"a|movie|{n}": f"can_{n}" for n in range(700)},
         "n": 3, "f": 1.5, "none": None, "flag": True, "s": "x\ny"},
        [1, 2, 3],
        "plain",
        {1: "int key"},
    ]
    for doc in docs:
        assert gentlejson.dumps(doc) == json.dumps(doc)


def test_the_fold_state_is_saved_with_the_gentle_writer(tmp_path, monkeypatch):
    from hub.db import DB

    db = DB(tmp_path / "hub.db")
    written = []
    real = gentlejson.dumps
    monkeypatch.setattr(gentlejson, "dumps", lambda value: written.append(value) or real(value))
    iptvcanon._save_items(db, [{"id": "can_1", "kind": "movie", "title": "Up", "sources": [
        {"provider": "a", "stream_id": "1"}]}], {})
    assert written and written[0]["by_source"] == {"a|movie|1": "can_1"}
    assert iptvcanon._state(db)["items"][0]["id"] == "can_1"


def test_the_hub_never_parses_back_a_state_it_just_saved(hub, monkeypatch):
    """A save keeps its own index: the matcher saved after each batch of model answers and every reader then
    parsed several MB again, starving plays of the interpreter lock (Nov 20 2026)."""
    db = hub.db
    iptvcanon._put_state(db, {"items": [{"id": "movie:a", "kind": "movie", "title": "A",
                                         "sources": [{"ref": "vod:p:1"}]}]})

    def no_parse(_db):
        raise AssertionError("parsed the state back")

    monkeypatch.setattr(iptvcanon, "_state_gently", no_parse)
    assert iptvcanon.index(db).by_ref["vod:p:1"]["id"] == "movie:a"
    assert iptvcanon.recent(db).by_id["movie:a"]["title"] == "A"


def test_threads_that_need_the_index_together_parse_it_once(hub, monkeypatch):
    import threading

    db = hub.db
    db.kv_set(iptvcanon.KV, {"items": [{"id": "movie:a", "kind": "movie", "title": "A", "sources": []}]})
    iptvcanon._INDEX.pop(id(db), None)
    real, parses = iptvcanon._state_gently, []

    def slow(d):
        parses.append(1)
        time.sleep(0.2)
        return real(d)

    monkeypatch.setattr(iptvcanon, "_state_gently", slow)
    found: list[object] = []
    threads = [threading.Thread(target=lambda: found.append(iptvcanon.index(db))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(parses) == 1
    assert found[0] is found[1] is found[2]


def test_a_read_does_not_wait_for_another_threads_transaction(tmp_path):
    import threading

    from hub.db import DB

    db = DB(tmp_path / "hub.db")
    db.kv_set("a", 1)
    inside, done = threading.Event(), threading.Event()

    def long_write():
        with db._lock:
            db._conn.execute("BEGIN")
            db._conn.execute("INSERT INTO hub_kv (key, value, updated_at) VALUES ('b', '2', 0)")
            inside.set()
            done.wait(5)
            db._conn.execute("COMMIT")

    writer = threading.Thread(target=long_write)
    writer.start()
    inside.wait(5)
    started = time.monotonic()
    assert db.kv_get("a") == 1  # read on this thread's own connection, not behind the writer's lock
    assert time.monotonic() - started < 1
    done.set()
    writer.join()


def test_setting_stamps_live_apart_from_the_values(tmp_path):
    """A stamp is read from its own small table: in hub_kv it sits after the value, and reading it walked a
    several-MB value's pages (0.3 s per read of the folded catalog's stamp, Nov 20 2026)."""
    from hub.db import DB

    path = tmp_path / "hub.db"
    db = DB(path)
    assert db.kv_stamp("big") is None
    db.kv_set_text("big", json.dumps({"items": ["x" * 100] * 1000}))
    first = db.kv_stamp("big")
    assert first is not None
    time.sleep(0.01)
    db.kv_set("big", {"items": []})
    assert db.kv_stamp("big") > first
    assert db.kv_get("big") == {"items": []}
    db.kv_delete("big")
    assert db.kv_stamp("big") is None and db.kv_get("big") is None
    # A row saved before the stamps table existed gets its stamp when the hub starts.
    db.execute("INSERT INTO hub_kv (key, value, updated_at) VALUES ('old', '1', 42.0)")
    assert DB(path).kv_stamp("old") == 42.0
