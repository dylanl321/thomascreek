"""A details page can ask whether Play would start now, without taking a login."""

from __future__ import annotations

from hub.iptvplay import lease

from tests.test_tv_api import _library
from tests.test_tv_phone import _login


def test_ready_then_busy_then_ready_again_for_the_holder(hub, people):
    _library(hub)
    _login(hub)
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    ask = {"movie_id": "vod:thomas-creek:101"}
    ready = dylan.client.get("/v1/tv/phone/available", headers=dylan.h, params=ask).json()
    assert ready["state"] == "ready" and ready["copies"] >= 1 and "reason" not in ready
    assert hub.db.kv_get("iptv.sessions") in (None, [])  # asking took no login
    assert dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json=ask).status_code == 200
    busy = avery.client.get("/v1/tv/phone/available", headers=avery.h, params=ask).json()
    assert busy["state"] == "busy" and "in use" in busy["reason"]
    # The phone that holds the login can start again on it.
    assert dylan.client.get("/v1/tv/phone/available", headers=dylan.h, params=ask).json()["state"] == "ready"


def test_unknown_titles_and_missing_ids_say_so(hub, people):
    _library(hub)
    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/tv/phone/available", headers=dylan.h).status_code == 400
    assert dylan.client.get("/v1/tv/phone/available", headers=dylan.h,
                            params={"movie_id": "vod:thomas-creek:nope"}).status_code == 404
    assert lease is not None
