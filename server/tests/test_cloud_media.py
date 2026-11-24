"""The cloud media worker: it joins with a code an admin approves, and phones get a lighter stream it makes
(hub/cloudmedia.py, Nov 23 2026)."""

from __future__ import annotations

import json
import time

from hub import cloudmedia
from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import SERVER, _login

SECRET = "worker-secret-for-tests-only"
PUBLIC = "https://media.example"


def _join(client, secret=SECRET):
    return client.post("/v1/cloud/join", json={"secret_hash": cloudmedia.secret_hash(secret), "name": "Cloud box"})


def _approved(hub, home_client, public=PUBLIC):
    made = _join(home_client).json()
    cloudmedia.approve(hub, made["join_id"])
    r = home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET},
                         json={"version": "1", "public_url": public, "sessions": 0, "capacity": 6, "load": 0.1})
    assert r.status_code == 200, r.text
    return r.json()


def _log(hub):
    return json.dumps([dict(row) for row in hub.db.execute("SELECT * FROM activity").fetchall()])


def test_a_worker_joins_only_from_home_and_only_when_approved(hub, home_client, away_client):
    assert _join(away_client).status_code == 403
    bad = home_client.post("/v1/cloud/join", json={"secret_hash": "not-a-hash"})
    assert bad.status_code == 400
    made = _join(home_client).json()
    assert made["status"] == "pending" and len(made["code"]) == 6
    assert _join(home_client).json()["code"] == made["code"]  # asking again is the same request
    assert home_client.get(f"/v1/cloud/join/{made['join_id']}").json()["status"] == "pending"
    assert SECRET not in json.dumps(hub.db.kv_get(cloudmedia.KV_JOINS)) and SECRET not in _log(hub)
    early = home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET}, json={})
    assert early.status_code == 401
    cloudmedia.approve(hub, made["join_id"])
    assert home_client.get(f"/v1/cloud/join/{made['join_id']}").json()["status"] == "approved"
    assert home_client.post("/v1/cloud/beat", headers={"x-tc-worker": "someone-else"}, json={}).status_code == 401
    assert home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET},
                            json={"public_url": PUBLIC}).json()["ok"] is True
    assert cloudmedia.status(hub)["up"] is True
    cloudmedia.forget(hub)
    assert home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET}, json={}).status_code == 401


def test_turning_a_request_away(hub, home_client):
    made = _join(home_client).json()
    cloudmedia.deny(hub, made["join_id"])
    assert home_client.get(f"/v1/cloud/join/{made['join_id']}").json()["status"] == "gone"
    assert cloudmedia.pending(hub) == []


def test_only_an_https_root_address_is_kept():
    assert cloudmedia._public_url("https://media.example/") == "https://media.example"
    for bad in ("http://media.example", "https://media.example/v", "https://u:p@media.example",
                "https://media.example?x=1", "media.example", ""):
        assert cloudmedia._public_url(bad) is None


def test_no_worker_no_lighter_stream(hub, people):
    _library(hub)
    _login(hub)
    avery = people["phones"]["Avery"]
    body = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                             json={"movie_id": "vod:thomas-creek:101", "data_saver": True}).json()
    assert "lite_url" not in body and body["url"].startswith("/v1/tv/phone/file/")


def test_the_play_answer_offers_the_lighter_stream(hub, people, home_client):
    _library(hub)
    _login(hub)
    _approved(hub, home_client)
    avery = people["phones"]["Avery"]
    plain = avery.client.post("/v1/tv/phone/play", headers=avery.h, json={"movie_id": "vod:thomas-creek:101"}).json()
    token = plain["url"].removeprefix("/v1/tv/phone/file/")
    assert plain["lite_url"] == f"{PUBLIC}/v/{token}/index.m3u8" and plain["lite_note"]
    assert "lite" not in plain
    saver = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                              json={"movie_id": "vod:thomas-creek:101", "data_saver": True}).json()
    assert saver["lite"] is True and saver["url"].startswith(f"{PUBLIC}/v/")
    assert saver["full_url"].startswith("/v1/tv/phone/file/")
    live = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                             json={"channel_key": "xc:thomas-creek:10", "data_saver": True}).json()
    assert live["url"].startswith(f"{PUBLIC}/v/") and "hls" not in live and live["ts_url"]


def test_a_grown_ups_direct_play_offers_it_too(hub, people, home_client):
    _library(hub)
    _login(hub)
    _approved(hub, home_client)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True,
                                   "data_saver": True}).json()
    assert body["url"].startswith(f"{PUBLIC}/v/") and body["full_url"].startswith(SERVER)


def test_switched_off_or_full_or_quiet_means_no_lighter_stream(hub, people, home_client):
    _library(hub)
    _login(hub)
    _approved(hub, home_client)
    avery = people["phones"]["Avery"]

    def lite():
        return "lite_url" in avery.client.post("/v1/tv/phone/play", headers=avery.h,
                                               json={"movie_id": "vod:thomas-creek:101"}).json()

    assert lite()
    cloudmedia.set_enabled(hub, False)
    assert not lite()
    cloudmedia.set_enabled(hub, True)
    home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET},
                     json={"public_url": PUBLIC, "sessions": 6, "capacity": 6})
    assert not lite()
    home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET},
                     json={"public_url": PUBLIC, "sessions": 0, "capacity": 6})
    assert lite()
    worker = hub.db.kv_get(cloudmedia.KV_WORKER)
    worker["beat_at"] = time.time() - cloudmedia.BEAT_FRESH_S - 1
    hub.db.kv_set(cloudmedia.KV_WORKER, worker)
    assert not lite()


def test_only_the_worker_opens_a_pass_and_nothing_is_logged(hub, people, home_client):
    _library(hub)
    _login(hub)
    _approved(hub, home_client)
    theo = people["phones"]["Theo"]  # a child: relayed, never the address, and the worker may still serve it
    played = theo.client.post("/v1/tv/phone/play", headers=theo.h,
                              json={"movie_id": "vod:thomas-creek:101", "data_saver": True})
    assert played.status_code == 200, played.text
    assert SERVER not in played.text and PASSWORD not in played.text
    token = cloudmedia.pass_of(played.json()["full_url"])
    assert token
    assert home_client.post("/v1/cloud/open", json={"pass": token}).status_code == 401
    assert theo.client.post("/v1/cloud/open", headers=theo.h, json={"pass": token}).status_code == 401
    opened = home_client.post("/v1/cloud/open", headers={"x-tc-worker": SECRET}, json={"pass": token})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["kind"] == "movie" and body["live"] is False and body["relay"] == f"/v1/tv/phone/file/{token}"
    assert body["sources"][0].startswith(f"{SERVER}/movie/household/") and body["sources"][0].endswith("/101.mp4")
    assert home_client.post("/v1/cloud/touch", headers={"x-tc-worker": SECRET},
                            json={"pass": token}).json()["ok"] is True
    assert home_client.post("/v1/cloud/open", headers={"x-tc-worker": SECRET},
                            json={"pass": "not-a-pass"}).status_code == 401
    logged = _log(hub)
    assert SERVER not in logged and PASSWORD not in logged and SECRET not in logged
    cloudmedia.set_enabled(hub, False)
    assert home_client.post("/v1/cloud/open", headers={"x-tc-worker": SECRET},
                            json={"pass": token}).status_code == 503


def test_a_live_pass_opens_the_ts_then_the_hls_address(hub, people, home_client):
    _library(hub)
    _login(hub)
    _approved(hub, home_client)
    avery = people["phones"]["Avery"]
    live = avery.client.post("/v1/tv/phone/play", headers=avery.h, json={"channel_key": "xc:thomas-creek:10"}).json()
    token = cloudmedia.pass_of(live["url"])
    body = home_client.post("/v1/cloud/open", headers={"x-tc-worker": SECRET}, json={"pass": token}).json()
    assert body["live"] is True and body["sources"][0].endswith("/10.ts") and body["sources"][-1].endswith("/10.m3u8")
    assert body["relay"] == f"/v1/tv/phone/live/{token}/stream.ts"


def test_the_web_page_shows_a_waiting_code(hub, home_client):
    from hub.routes.cloud import _page_html

    made = _join(home_client).json()
    page = _page_html(hub, {"sid": "test-session"}, None)
    assert made["code"] in page and "Approve" in page and SECRET not in page


def test_a_beat_saves_its_row_off_the_event_loop(hub, home_client, monkeypatch):
    import asyncio
    import threading

    made = _join(home_client).json()
    cloudmedia.approve(hub, made["join_id"])
    where: list[tuple[str, bool]] = []
    real = cloudmedia.beat

    def beat(h, body, now=None):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        where.append((threading.current_thread().name, on_loop))
        return real(h, body, now)

    monkeypatch.setattr(cloudmedia, "beat", beat)
    r = home_client.post("/v1/cloud/beat", headers={"x-tc-worker": SECRET}, json={"public_url": PUBLIC})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert where and where[0][1] is False
    assert cloudmedia.status(hub)["up"] is True
