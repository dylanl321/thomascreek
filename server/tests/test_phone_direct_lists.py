"""A grown-up's phone reads film and series lists and pages straight from the provider (Dylan, Nov 19 2026); kids
and TVs keep reading through the hub, and every stream still leases a login through /play."""

from __future__ import annotations

import json

import respx
from httpx import Response

from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import SERVER, _login


def _activity(hub) -> str:
    return json.dumps([dict(row) for row in hub.db.execute("SELECT * FROM activity").fetchall()])


def test_an_adults_phone_gets_the_provider_login(hub, people):
    _login(hub)
    dylan = people["phones"]["Dylan"]
    r = dylan.client.get("/v1/tv/phone/logins", headers=dylan.h)
    assert r.status_code == 200, r.text
    (main,) = r.json()["providers"]
    assert main["id"] == "thomas-creek" and main["server"] == SERVER and main["servers"] == [SERVER]
    assert main["username"] == "household" and main["password"] == PASSWORD
    logged = _activity(hub)
    assert "tv.phone.providers" in logged and PASSWORD not in logged and SERVER not in logged


def test_a_child_never_gets_the_provider_login(hub, people):
    _login(hub)
    theo = people["phones"]["Theo"]
    r = theo.client.get("/v1/tv/phone/logins", headers=theo.h)
    assert r.status_code == 403 and PASSWORD not in r.text and SERVER not in r.text


def test_no_login_says_so(hub, people):
    dylan = people["phones"]["Dylan"]
    r = dylan.client.get("/v1/tv/phone/logins", headers=dylan.h)
    assert r.status_code == 503 and r.json()["error"] == "no_provider"


def test_an_episode_the_phone_read_itself_plays(hub, people):
    """The hub hasn't read Drama Show's episodes; the phone did, from the provider. /play reads the series once."""
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    assert hub.tv_catalog.episode("ep:thomas-creek:950") is None
    payload = {"info": {"name": "Drama Show"}, "episodes": {"1": [
        {"id": "950", "episode_num": 1, "title": "One", "container_extension": "mkv",
         "info": {"duration_secs": 1800}}]}}
    with respx.mock(assert_all_called=False) as mock:
        api = mock.get(f"{SERVER}/player_api.php").mock(return_value=Response(200, json=payload))
        r = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={
            "episode_id": "ep:thomas-creek:950", "series_id": "series:thomas-creek:51",
            "player": "exo", "direct": True})
    assert r.status_code == 200, r.text
    assert api.called and r.json()["url"].endswith("/950.mkv")


def test_a_child_cant_play_an_unknown_episode_by_naming_its_series(hub, people):
    _library(hub)
    _login(hub)
    theo = people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        api = mock.get(f"{SERVER}/player_api.php").mock(return_value=Response(200, json={}))
        r = theo.client.post("/v1/tv/phone/play", headers=theo.h, json={
            "episode_id": "ep:thomas-creek:950", "series_id": "series:thomas-creek:51",
            "player": "exo", "direct": True})
    assert r.status_code == 404 and not api.called
