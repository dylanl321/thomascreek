"""A login is never called busy on an old count, a phone that starts again reuses its own login, and a busy
answer says who holds it (Nov 19 2026: plays waited "finding a free login" until the phone gave up)."""

from __future__ import annotations

import time

from hub.iptvplay import USAGE_FRESH_S, _view, busy_reason, lease, note_usage

from tests.test_tv_api import _library
from tests.test_tv_phone import _login


def _full(hub, at):
    note_usage(hub, "thomas-creek", "main", {"max_connections": "1", "active_cons": "1", "status": "Active"}, now=at)


def test_an_old_provider_count_does_not_keep_a_login_full(hub):
    now = time.time()
    login = {"provider": "thomas-creek", "login": "main"}
    fresh = {"max_connections": 1, "active_cons": 1, "at": now - 10}
    old = {**fresh, "at": now - USAGE_FRESH_S - 1}
    assert _view(login, fresh, 0, now)["status"] == "full"
    assert _view(login, old, 0, now)["status"] == "ok"
    assert _view(login, old, 1, now)["status"] == "full"  # the hub's own lease still counts


def test_a_phone_that_starts_again_keeps_its_own_login(hub, people):
    _library(hub)
    _login(hub)
    holder = "phone:dev1"
    assert lease(hub, "vod:thomas-creek:101", holder) is not None
    _full(hub, time.time())  # the provider still counts the stream this phone just had
    again = lease(hub, "vod:thomas-creek:101", holder)
    assert again is not None and again["login"] == "main"
    # Someone else is still turned away while the provider and the pool both say it's in use.
    assert lease(hub, "vod:thomas-creek:101", "phone:dev2") is None


def test_busy_says_who_holds_the_login(hub, people):
    _library(hub)
    _login(hub)
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    assert dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": "vod:thomas-creek:101"}).status_code == 200
    busy = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                             json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert busy.status_code == 503
    assert "a phone" in busy.json()["message"] and "in use" in busy.json()["message"]
    assert "phone" in busy_reason(hub, "vod:thomas-creek:101")


def test_a_play_says_how_long_each_step_took(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    for direct in (False, True):
        r = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                              json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": direct})
        assert r.status_code == 200
        steps = [part.split(";")[0] for part in r.headers["Server-Timing"].split(", ")]
        assert steps == ["ready", "row", "lease", "resume", "total"]  # total: every answer (hub.timings)
