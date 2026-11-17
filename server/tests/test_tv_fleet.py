"""A TV reports how it is, and an admin can ask it to do one thing."""

from __future__ import annotations

from datetime import datetime, timezone

from hub.routes.home import shape_event
from hub.tvapp_common import KV_TVS
from hub.tvfleet import devices, report, sweep

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc).timestamp()
BODY = {
    "capability": "owner", "model": "onn. 4K Streaming Box", "android": "12", "platform": "google_tv",
    "app_version": "1.5.0", "cpu_pct": 12, "ram": {"used_mb": 800, "total_mb": 2048},
    "storage": {"used_mb": 3200, "total_mb": 8000}, "temperature_c": 48,
    "network": {"kind": "wifi", "ssid": "House", "rssi_dbm": -55}, "ping_ms": 4,
    "foreground": "me.dlewis.thomascreek.tv", "uptime_s": 86400, "remote_battery_pct": 80,
    "packages": {"hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "changed": True, "list": []},
}


def _tv(hub, home_client):
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    token = _token(home_client, key, Clock())["access_token"]
    return _auth(token)


def test_a_check_in_is_the_fleet_row(hub, home_client):
    _tv(hub, home_client)
    answer = report(hub, TV_ID, BODY, NOW)
    assert answer == {
        "ok": True, "need_packages": False,
        "kill_list": ["com.adups.fota", "com.adups.fota.sysoper", "com.fw.upgrade.sysoper", "com.swe.dgbluancher"],
        "policy": {"blocked": [], "restrict_settings": False, "hide_status_bar": False, "disable_keyguard": False},
        "commands": [],
    }
    listed = devices(hub, NOW)
    assert listed == {"devices": [{
        "id": TV_ID, "name": "Living room", "room": "living_room", "online": True,
        "capability": "owner", "model": "onn. 4K Streaming Box", "android": "12", "platform": "google_tv",
        "app_version": "1.5.0", "health": "ok", "last_seen": "2026-11-14T18:00:00+00:00",
        "cpu_pct": 12, "ram_pct": 39, "storage_pct": 40, "temperature_c": 48,
        "network": {"kind": "wifi", "ssid": "House", "rssi_dbm": -55}, "ping_ms": 4,
        "foreground": "me.dlewis.thomascreek.tv", "uptime_s": 86400,         "remote_battery_pct": 80,
    }]}


def test_the_house_hears_once_and_a_command_reaches_only_that_tv(hub, people, home_client):
    headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    posted = home_client.post("/v1/tv/fleet/report", headers=headers, json=BODY)
    assert posted.status_code == 200, posted.text
    assert posted.json()["need_packages"] is False
    assert people["phones"]["Avery"].client.get("/v1/tv/fleet", headers=people["phones"]["Avery"].h).status_code == 403
    assert dylan.client.post("/v1/tv/fleet/report", headers=dylan.h, json=BODY).status_code == 403
    assert home_client.post("/v1/tv/fleet/report", headers=headers, json={"capability": "maybe"}).json()["message"] == "The TV didn't say what it can do."

    hot = dict(BODY)
    hot["temperature_c"] = 80
    home_client.post("/v1/tv/fleet/report", headers=headers, json=hot)
    home_client.post("/v1/tv/fleet/report", headers=headers, json=hot)
    assert list(hub.db.kv_get("tv.fleet.notices")) == [f"{TV_ID}|hot"]

    subject = hub.tv_app.subject_for(hub.tv_app.tvs()[TV_ID], TV_ID, None)
    screen = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), None, device_id=f"tv:{TV_ID}", tv=True)
    sent = dylan.client.post("/v1/tv/fleet/" + TV_ID + "/command", headers=dylan.h, json={"action": "reboot", "params": {}})
    assert sent.status_code == 200, sent.text
    event = screen.queue.get_nowait()
    assert event["type"] == "tv_manage" and event["data"]["action"] == "reboot" and event["data"]["id"] == sent.json()["id"]
    waiting = home_client.get("/v1/tv/manage", headers=headers, params={"timeout": 0})
    assert waiting.json()["commands"][0]["id"] == sent.json()["id"]
    acked = home_client.post("/v1/tv/manage/ack", headers=headers, json={"id": sent.json()["id"], "ok": False, "detail": "This TV can't restart itself."})
    assert acked.json() == {"ok": True}
    assert home_client.post("/v1/tv/manage/ack", headers=headers, json={"id": sent.json()["id"], "ok": True}).json() == {
        "ok": False, "message": "That command has already finished.",
    }
    assert dylan.client.post("/v1/tv/fleet/" + TV_ID + "/command", headers=dylan.h, json={"action": "fly", "params": {}}).json()["message"] == "That isn't a command this TV knows."
    kept = dylan.client.put("/v1/tv/fleet/kill-list", headers=dylan.h, json={"packages": ["me.dlewis.thomascreek.tv"]})
    assert kept.json()["message"] == "That app stays on the TV."
    bad = dylan.client.put("/v1/tv/fleet/kill-list", headers=dylan.h, json={"packages": ["not a package"]})
    assert bad.json()["message"] == "That doesn't look like an app."
    saved = dylan.client.put("/v1/tv/fleet/kill-list", headers=dylan.h, json={"packages": ["com.example.ads"]})
    assert saved.status_code == 200, saved.text
    again = home_client.get("/v1/tv/manage", headers=headers, params={"timeout": 0}).json()
    assert again["commands"][0]["action"] == "kill_list" and again["commands"][0]["params"]["packages"] == ["com.example.ads"]

    chart = dylan.client.get("/v1/tv/fleet/" + TV_ID + "/history", headers=dylan.h, params={"metric": "mood"})
    assert chart.json()["message"] == "That chart isn't one the hub keeps."
    quiet = dict(BODY)
    quiet["temperature_c"] = 48
    report(hub, TV_ID, quiet, NOW)
    sweep(hub, NOW + 31 * 60)
    assert hub.db.kv_get("tv.fleet.notices") == {f"{TV_ID}|offline": True}


def test_a_tv_reporting_from_the_background_or_beating_from_the_app_is_online(hub, home_client):
    _tv(hub, home_client)
    _stamp(hub, state_at=0, last_seen_at=0)
    report(hub, TV_ID, BODY, NOW)
    # The app is closed: only the 15-minute background report arrives, and the TV still reads online.
    row = devices(hub, NOW + 10 * 60)["devices"][0]
    assert row["online"] is True and row["health"] == "ok"
    assert devices(hub, NOW + 25 * 60)["devices"][0]["online"] is False
    # The app is open: its heartbeat is newer than the last report, so it decides.
    _stamp(hub, state_at=NOW + 40 * 60)
    row = devices(hub, NOW + 41 * 60)["devices"][0]
    assert row["online"] is True and row["health"] == "ok"
    assert row["last_seen"] == "2026-11-14T18:40:00+00:00"
    sweep(hub, NOW + 41 * 60)
    assert f"{TV_ID}|offline" not in list(hub.db.kv_get("tv.fleet.notices") or {})


def _stamp(hub, **times):
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(times)
    hub.db.kv_set(KV_TVS, tvs)
