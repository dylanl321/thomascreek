"""Live lineup rows carry the folded catalog id that POST /v1/tv/tune takes."""

from __future__ import annotations

from hub.iptvcanon import KV, _put_state


def test_live_rows_carry_the_tune_id(hub, people):
    hub.tv_catalog.ingest(live=[
        {"stream_id": 1, "name": "US| ABC 11 RALEIGH WTVD HD", "category_id": "1"},
        {"stream_id": 10, "name": "US| ESPN HD", "category_id": "1"},
    ], live_categories=[{"category_id": "1", "category_name": "US| LOCALS"}])
    by_source = hub.db.kv_get(KV)["by_source"]
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tv/lineup", headers=dylan.h)
    assert body.status_code == 200, body.text
    rows = {row["key"]: row for row in body.json()["channels"]}
    assert rows
    for key, row in rows.items():
        stream_id = key.split(":")[-1]
        assert row["canonical_id"] == by_source[f"thomas-creek|live|{stream_id}"]
        assert row["canonical_id"].startswith("can_")


def test_a_channel_the_catalog_has_not_folded_has_no_tune_id(hub, people):
    hub.tv_catalog.ingest(live=[{"stream_id": 7, "name": "US| CNN", "category_id": "1"}],
                          live_categories=[{"category_id": "1", "category_name": "US| NEWS"}])
    _put_state(hub.db, {"items": [], "by_source": {}})  # how the hub saves the fold
    dylan = people["phones"]["Dylan"]
    rows = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()["channels"]
    assert rows and all("canonical_id" not in row for row in rows)
