"""Kids' TV bedtime: a limit and a night window, with a warning before either one."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from hub.routes.home import shape_event

from tests.test_tv_api import KID_TV, Key, _enroll

TZ = ZoneInfo("America/New_York")


def _at(hour: int, minute: int, day: int = 14) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_kids_start_with_a_limit_and_lock_at_bedtime(hub, home_client, people):
    hub.home.states["scene.good_night"] = {
        "entity_id": "scene.good_night", "state": "scening",
        "attributes": {"friendly_name": "Good night"},
    }
    hub.config.hub.live_actions = ["scene.run"]
    dylan = people["phones"]["Dylan"]
    rules = dylan.client.get("/v1/tv/bedtime", headers=dylan.h).json()
    assert rules["profile"] == "kids"
    assert rules["daily_minutes"] == 90 and rules["from"] == "20:30" and rules["to"] == "07:00"
    assert rules["warn_minutes"] == 10 and rules["scene"] == "scene.good_night"

    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    phones = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id)
    sent = asyncio.run(hub.tv_bedtime.tick(_at(20, 40)))
    assert sent[0]["state"] == "locked" and sent[0]["profile"] == "kids"
    assert phones.queue.get_nowait()["data"]["state"] == "locked"
    assert ("scene", "turn_on", {"entity_id": "scene.good_night"}) in hub.calls
    asyncio.run(hub.tv_bedtime.tick(_at(20, 41)))
    assert hub.calls.count(("scene", "turn_on", {"entity_id": "scene.good_night"})) == 1

    assert hub.tv_bedtime.extend("kids", 15, _at(20, 40))["state"] == "unlocked"
    grown = dylan.client.post("/v1/tv/bedtime/extend", headers=dylan.h, json={"profile": "kids", "minutes": 15})
    assert grown.status_code == 200, grown.text
    assert people["phones"]["Theo"].client.post("/v1/tv/bedtime/extend", headers=people["phones"]["Theo"].h,
                                                json={"profile": "kids", "minutes": 15}).status_code == 403
    assert people["phones"]["Avery"].client.put("/v1/tv/bedtime", headers=people["phones"]["Avery"].h,
                                                json={"profile": "kids", "daily_minutes": 30}).status_code == 403


def test_a_warning_comes_before_the_minutes_run_out(hub, people):
    hub.db.kv_set("tv.bedtime.usage", {"kids": {"day": "2026-11-14", "minutes": 80}})
    afternoon = _at(15, 0)
    sent = asyncio.run(hub.tv_bedtime.tick(afternoon))
    assert sent[0]["state"] == "warning" and sent[0]["minutes_left"] == 10
    hub.db.kv_set("tv.bedtime.usage", {"kids": {"day": "2026-11-14", "minutes": 90, "announced": "warning"}})
    sent = asyncio.run(hub.tv_bedtime.tick(afternoon))
    assert sent[0]["state"] == "locked" and sent[0]["minutes_left"] == 0
    assert hub.calls == []


def test_a_minute_counts_only_while_the_kids_tv_is_playing(hub, home_client):
    key = Key()
    _enroll(home_client, key, KID_TV, "Theo's TV")
    hub.tv_app.set_tv(KID_TV, status="approved", profile="kids", room="theo_room")
    tvs = hub.tv_app.tvs()
    tvs[KID_TV]["playing"] = {"title": "Blue"}
    tvs[KID_TV]["foreground"] = True
    tvs[KID_TV]["state_at"] = _at(15, 0).timestamp()
    hub.tv_app._save_tvs(tvs)
    asyncio.run(hub.tv_bedtime.tick(_at(15, 0)))
    assert hub.tv_bedtime._used("kids", _at(15, 0)) == 1
    tvs[KID_TV]["playing"] = None
    hub.tv_app._save_tvs(tvs)
    asyncio.run(hub.tv_bedtime.tick(_at(15, 1)))
    assert hub.tv_bedtime._used("kids", _at(15, 1)) == 1
