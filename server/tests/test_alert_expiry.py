"""Door and camera alerts leave the Lock Screen by themselves; a doorbell Live Activity is ended by the hub."""

from __future__ import annotations

import asyncio
import time

from hub.push import PushResult


def _arm(hub, people):
    hub.vault.set("apns.auth_key_p8", "k")
    hub.vault.set("apns.key_id", "ABC")
    hub.config.apns.team_id = "T"
    sent = []

    async def fake_send(token, payload, **kw):
        sent.append((token, payload, kw))
        return PushResult(True, 200)

    hub.apns.send = fake_send  # type: ignore[method-assign]
    hub.db.update("devices", "id", people["phones"]["Dylan"].device_id, {"apns_token": "ab12", "apns_env": "sandbox"})
    return sent


async def test_camera_alerts_expire_and_get_cleared(hub, people, monkeypatch):
    import hub.notify as notify

    sent = _arm(hub, people)
    monkeypatch.setitem(notify.EXPIRES, "cameras", 1)
    monkeypatch.setattr(notify, "CLEAR_GRACE_S", 0)
    await hub.notify.send(title="Person in the driveway", body=None, cat="cameras", person_ids=[people["dylan"]["id"]])
    alert = sent[0][1]
    assert alert["expires"] <= time.time() + 2
    await asyncio.wait_for(hub.notify._clear_task, 3)
    clear = sent[-1]
    assert clear[1] == {"aps": {"content-available": 1}, "clear": 1}
    assert clear[2]["push_type"] == "background" and clear[2]["priority"] == 5


async def test_doorbell_alert_is_shorter_and_weather_does_not_expire(hub, people):
    sent = _arm(hub, people)
    await hub.notify.send(title="Someone's at the front door", body=None, cat="cameras", group_key="doorbell",
                          person_ids=[people["dylan"]["id"]], level="act_now")
    await hub.notify.send(title="Storm warning", body=None, cat="weather", person_ids=[people["dylan"]["id"]])
    assert 500 < sent[0][1]["expires"] - time.time() <= 600
    assert "expires" not in sent[1][1]
    hub.notify._clear_task.cancel()


async def test_doorbell_live_activity_is_ended(hub, people, monkeypatch):
    import hub.household as household

    sent = _arm(hub, people)
    dev = people["phones"]["Dylan"].device_id
    hub.household.set_live_token(dev, "start1", "sandbox")
    monkeypatch.setattr(household, "DOORBELL_LIVE_S", -4.9)
    assert await hub.household.doorbell_live([people["dylan"]["id"]], "front", "Front door") == 1
    start = sent[-1][1]["aps"]
    assert start["event"] == "start" and start["dismissal-date"] <= time.time()
    # The phone reports the ring's own update token; the hub ends it.
    hub.household.add_doorbell_activity(dev, "act1", "upd1", "sandbox")
    await asyncio.wait_for(hub.household._door_end, 3)
    end = sent[-1]
    assert end[0] == "upd1" and end[1]["aps"]["event"] == "end" and end[2]["push_type"] == "liveactivity"
    assert hub.db.kv_get(f"la_door.{dev}") == []


def test_phone_sends_a_rings_update_token(hub, people):
    ph = people["phones"]["Dylan"]
    hub.household.set_live_token(ph.device_id, "start1", "production")
    r = ph.client.post("/v1/devices/live-activity", json={"doorbell_activity_token": "abcd", "activity_id": "A"},
                       headers=ph.h)
    assert r.status_code == 200
    assert hub.db.kv_get(f"la_door.{ph.device_id}")[0]["token"] == "abcd"
    assert hub.db.kv_get(f"la_start.{ph.device_id}")["token"] == "start1"  # the start token is kept
