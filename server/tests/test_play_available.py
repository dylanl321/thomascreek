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


def test_an_episode_read_straight_from_the_provider_answers_for_its_series(hub, people):
    # Phone 0.13.0 walkthrough: show pages asked with an episode id the hub hadn't read and got 404.
    _library(hub)
    _login(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    ask = {"episode_id": "ep:thomas-creek:99999", "series_id": "series:thomas-creek:51"}
    got = dylan.client.get("/v1/tv/phone/available", headers=dylan.h, params=ask)
    assert got.status_code == 200 and got.json()["state"] == "ready"
    assert dylan.client.get("/v1/tv/phone/available", headers=dylan.h,
                            params={"episode_id": "ep:thomas-creek:99999"}).status_code == 404
    # A child never gets an answer this way (the kids' rules read only episodes the hub knows).
    assert theo.client.get("/v1/tv/phone/available", headers=theo.h, params=ask).status_code == 404
