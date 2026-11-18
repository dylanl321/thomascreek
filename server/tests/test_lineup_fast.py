"""The full lineup is the biggest body a TV reads at launch: gzip for it, and a 304 while it is unchanged."""
from __future__ import annotations

import json


def _ingest(hub, n: int) -> None:
    hub.tv_catalog.ingest(
        live=[{"stream_id": i, "name": f"US| CHANNEL {i} HD", "category_id": "1"} for i in range(1, n + 1)],
        live_categories=[{"category_id": "1", "category_name": "US| ENTERTAINMENT"}],
    )


def test_a_large_lineup_is_gzipped_for_a_caller_that_takes_gzip(hub, people):
    _ingest(hub, 600)
    dylan = people["phones"]["Dylan"]
    res = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "Accept-Encoding": "gzip"})
    assert res.status_code == 200, res.text
    assert res.headers["content-encoding"] == "gzip"
    assert res.headers["vary"] == "Accept-Encoding"
    assert len(res.json()["channels"]) == 600  # the test client unpacks it, like OkHttp and URLSession do


def test_a_caller_without_gzip_gets_plain_json_and_the_same_etag(hub, people):
    _ingest(hub, 600)
    dylan = people["phones"]["Dylan"]
    zipped = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "Accept-Encoding": "gzip"})
    plain = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.json() == zipped.json()
    assert plain.headers["etag"] == zipped.headers["etag"]


def test_an_unchanged_lineup_is_a_304_with_no_body(hub, people):
    _ingest(hub, 600)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/lineup", headers=dylan.h)
    again = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""


def test_a_small_body_is_not_gzipped(hub, people):
    _ingest(hub, 1)
    dylan = people["phones"]["Dylan"]
    res = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "Accept-Encoding": "gzip"})
    assert "content-encoding" not in res.headers
    assert json.loads(res.content)["channels"]


def test_the_lineup_on_the_wire_is_a_fifth_of_the_json_or_less(hub, people):
    _ingest(hub, 2000)
    dylan = people["phones"]["Dylan"]
    res = dylan.client.get("/v1/tv/lineup", headers={**dylan.h, "Accept-Encoding": "gzip"})
    assert res.num_bytes_downloaded * 5 <= len(res.content)


def test_a_repeat_lineup_or_groups_call_does_not_rebuild_or_reread_the_id_map(hub, people, monkeypatch):
    """The live hub took 4-5 s a call: every call decoded the large canonical-id map to check its cache."""
    _ingest(hub, 600)
    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/tv/lineup", headers=dylan.h).status_code == 200
    assert dylan.client.get("/v1/tv/groups", headers=dylan.h).status_code == 200
    calls = []
    real = hub.tv_catalog._live_canon
    monkeypatch.setattr(hub.tv_catalog, "_live_canon", lambda: calls.append(1) or real())

    def rebuilt(*_a, **_k):
        raise AssertionError("rebuilt")

    monkeypatch.setattr(hub.tv_catalog, "_build_lineup", rebuilt)
    again = dylan.client.get("/v1/tv/lineup", headers=dylan.h)
    groups = dylan.client.get("/v1/tv/groups", headers=dylan.h)
    assert again.status_code == 200 and len(again.json()["channels"]) == 600
    assert groups.status_code == 200 and groups.json()["countries"]
    assert calls == []
    assert again.headers["server-timing"].startswith("app;dur=")


def test_a_lineup_edit_and_a_health_report_show_on_the_next_call(hub, people):
    _ingest(hub, 3)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    key = first["channels"][0]["key"]
    hub.db.kv_set("tvapp.lineup", {"channels": [{"key": key, "name": "Renamed by hand"}]})
    renamed = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    assert next(row for row in renamed["channels"] if row["key"] == key)["name"] == "Renamed by hand"
    from hub.channelhealth import KV
    hub.db.kv_set(KV, {key: {"main": {"bits": [1, 1, 1], "last_ok": 1.0}}})
    healthy = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    assert next(row for row in healthy["channels"] if row["key"] == key)["health"]["score"] == 1.0
    hub.db.kv_set(KV, {})
    cleared = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    assert "health" not in next(row for row in cleared["channels"] if row["key"] == key)
