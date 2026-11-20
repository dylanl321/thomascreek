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
