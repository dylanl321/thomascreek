"""Opt-in alerts: Frigate, doorbell, Home Assistant, quiet hours, automations."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest


@pytest.fixture
def sent(hub, people):
    """Capture what would be pushed (inbox rows are still written)."""
    hub.frigate._info = {"140_frontdoor": {"enabled": True}, "140_driveway": {"enabled": True}}
    for p in ("dylan", "avery", "theo"):
        hub.alerts.set_prefs(people[p]["id"], {"on": True})
    out = []
    orig = hub.notify.send

    async def send(**kw):
        out.append(kw)
        return await orig(**kw)

    hub.notify.send = send  # type: ignore[method-assign]
    return out


def ev(cam="140_frontdoor", label="person", sub=None, eid=None):
    return {"id": eid or f"{time.time()}-{cam}", "camera": cam, "label": label, "sub_label": sub,
            "has_snapshot": True, "start_time": time.time()}


def who(hub, rows):
    return sorted(hub.db.person(r["person_ids"][0])["name"] for r in rows)


async def test_off_until_switched_on(hub, people):
    hub.frigate._info = {"140_frontdoor": {"enabled": True}}
    assert hub.alerts.prefs(people["avery"]["id"])["on"] is False
    assert await hub.alerts.on_frigate(ev()) == 0
    hub.alerts.set_prefs(people["avery"]["id"], {"on": True})
    assert await hub.alerts.on_frigate(ev()) == 1


async def test_frigate_defaults_front_door_person_adults_only(hub, sent):
    assert await hub.alerts.on_frigate(ev()) == 2
    assert who(hub, sent) == ["Avery", "Dylan"]
    assert sent[0]["image"].endswith("/snapshot.jpg") and sent[0]["ref"] == "thomascreek://camera/140_frontdoor"
    # Driveway and cars are off by default
    assert await hub.alerts.on_frigate(ev("140_driveway")) == 0
    assert await hub.alerts.on_frigate(ev(label="car")) == 0


async def test_frigate_cooldown_known_faces_and_per_person_choice(hub, people, sent):
    now = time.time()
    assert await hub.alerts.on_frigate(ev(), now=now) == 2
    assert await hub.alerts.on_frigate(ev(), now=now + 60) == 0          # within 5 minutes
    assert await hub.alerts.on_frigate(ev(), now=now + 301) == 2
    assert await hub.alerts.on_frigate(ev(sub="Dylan"), now=now + 900) == 0  # a face Frigate knows
    hub.alerts.set_prefs(people["avery"]["id"], {"cameras": {"140_driveway": ["car", "nonsense"]}, "gap_s": 60})
    assert hub.alerts.prefs(people["avery"]["id"])["cameras"] == {"140_driveway": ["car"]}
    sent.clear()
    assert await hub.alerts.on_frigate(ev("140_driveway", "truck"), now=now + 1000) == 1
    assert who(hub, sent) == ["Avery"] and sent[0]["title"].startswith("Truck at the ")


async def test_only_when_nobody_home(hub, people, sent):
    hub.alerts.set_prefs(people["dylan"]["id"], {"only_away": True})
    hub.home.states["zone.home"] = {"entity_id": "zone.home", "state": "2", "attributes": {}}
    assert await hub.alerts.on_frigate(ev()) == 1  # Avery only
    hub.home.states["zone.home"]["state"] = "0"
    sent.clear()
    assert await hub.alerts.on_frigate(ev(), now=time.time() + 400) == 2


async def test_doorbell_rings_through_quiet_hours(hub, people, sent):
    hub.home.states["event.front_doorbell"] = {"entity_id": "event.front_doorbell", "state": "2026-11-12T10:00:00",
                                               "attributes": {"device_class": "doorbell", "friendly_name": "Doorbell"}}
    hub.alerts.set_prefs(people["dylan"]["id"], {"quiet": {"on": True, "from": "00:00", "to": "23:59"}})
    hub.alerts.set_prefs(people["avery"]["id"], {"quiet": {"on": True, "from": "00:00", "to": "23:59", "ring_urgent": False}})
    n = await hub.alerts.on_state("event.front_doorbell", {"state": "2026-11-12T10:00:00"},
                                  {"state": "2026-11-12T10:05:00", "attributes": {"device_class": "doorbell"}})
    assert n == 2
    by = {hub.db.person(r["person_ids"][0])["name"]: r for r in sent}
    assert by["Dylan"]["silent"] is False and by["Dylan"]["level"] == "act_now"
    assert by["Avery"]["silent"] is True
    assert "Theo" not in by


async def test_hazards_reach_kids_too_and_garage_left_open(hub, people, sent):
    hub.home.states["binary_sensor.laundry_leak"] = {"entity_id": "binary_sensor.laundry_leak", "state": "off",
                                                     "attributes": {"device_class": "moisture", "friendly_name": "Laundry leak"}}
    hub.alerts.set_prefs(people["theo"]["id"], {"hazards": True})
    n = await hub.alerts.on_state("binary_sensor.laundry_leak", {"state": "off"},
                                  {"state": "on", "attributes": {"device_class": "moisture"}})
    assert n == 3 and sent[0]["title"].startswith("Water leak")
    sent.clear()
    opened = datetime.fromtimestamp(time.time() - 700, timezone.utc).isoformat()
    hub.home.states["cover.large_garage"] = {**hub.home.states["cover.large_garage"], "state": "open", "last_changed": opened}
    assert await hub.alerts.check_garages() == 2
    assert await hub.alerts.check_garages() == 0  # once per opening
    assert sent[0]["push_category"] == "SECURE"


async def test_extra_entities_are_offered_by_dylan_then_chosen(hub, people, sent):
    hub.home.states["sensor.freezer"] = {"entity_id": "sensor.freezer", "state": "2",
                                         "attributes": {"unit_of_measurement": "°F", "friendly_name": "Freezer"}}
    hub.alerts.set_prefs(people["avery"]["id"], {"entities": {"sensor.freezer": True}})
    assert hub.alerts.prefs(people["avery"]["id"])["entities"] == {}  # not offered yet
    hub.alerts.set_sources({"extra": [{"entity_id": "sensor.freezer", "name": "Freezer temperature"}]})
    hub.alerts.set_prefs(people["avery"]["id"], {"entities": {"sensor.freezer": True}})
    n = await hub.alerts.on_state("sensor.freezer", {"state": "2"}, {"state": "15", "attributes": {"unit_of_measurement": "°F"}})
    assert n == 1 and sent[0]["body"] == "Now 15 °F"


def test_settings_api_by_role(hub, people, home_client):
    ph = people["phones"]
    hub.frigate._info = {"140_frontdoor": {"enabled": True}}
    r = home_client.get("/v1/alerts", headers=ph["Dylan"].h).json()
    assert r["prefs"]["cameras"] == {"140_frontdoor": ["person"]} and "sources" in r and r["cameras"][0]["id"] == "140_frontdoor"
    assert not {c["id"] for c in r["categories"]} & {"cameras", "doors", "house"}
    kid = home_client.get("/v1/alerts", headers=ph["Theo"].h).json()
    assert "cameras" not in kid["prefs"] and "cameras" not in kid and kid["prefs"]["quiet"]["on"] is True
    # A child can't switch camera alerts on
    home_client.put("/v1/alerts", json={"prefs": {"doorbell": True, "cameras": {"140_frontdoor": ["person"]}}},
                    headers=ph["Theo"].h)
    assert hub.alerts.prefs(people["theo"]["id"]).get("cameras") == {"140_frontdoor": ["person"]}  # default, unused
    r = home_client.put("/v1/alerts", json={"prefs": {"gap_s": 900, "quiet": {"on": True, "from": "23:00"}},
                                            "device_prefs": {"holiday": True}}, headers=ph["Avery"].h).json()
    assert r["prefs"]["gap_s"] == 900 and r["prefs"]["quiet"]["from"] == "23:00" and r["device_prefs"]["holiday"] is True
    assert home_client.get("/v1/alerts/entities", headers=ph["Avery"].h).status_code == 403
    assert home_client.put("/v1/alerts/sources", json={"outside_doors": ["lock.front_door"]},
                           headers=ph["Dylan"].h).json()["sources"]["outside_doors"] == ["lock.front_door"]


def test_automation_webhook(hub, people, home_client, away_client):
    hub.vault.set("alerts.webhook_token", "tok123")
    for p in ("avery", "theo"):
        hub.alerts.set_prefs(people[p]["id"], {"on": True})
    body = {"title": "Freezer is warm", "message": "15°F", "to": ["Avery", "kids"]}
    assert home_client.post("/v1/alerts/notify", json=body).status_code == 401
    assert away_client.post("/v1/alerts/notify", json=body, headers={"Authorization": "Bearer tok123"}).status_code == 403
    r = home_client.post("/v1/alerts/notify", json=body, headers={"Authorization": "Bearer tok123"})
    assert r.json() == {"ok": True, "people": 2}
    titles = [x["title"] for x in hub.db.all("SELECT * FROM inbox WHERE person_id=?", [people["theo"]["id"]])]
    assert titles == ["Freezer is warm"]


def test_automation_snippet_needs_face_id(people, home_client):
    ph = people["phones"]["Dylan"]
    r = ph.secure_post("/v1/alerts/automation", {})
    assert r.status_code == 200 and "rest_command" in r.json()["yaml"] and "Bearer " in r.json()["yaml"]
    assert people["phones"]["Avery"].secure_post("/v1/alerts/automation", {}).status_code == 403


def test_quiet_window_wraps_midnight(hub):
    q = {"on": True, "from": "22:00", "to": "06:30"}
    tz = hub.config.hub.timezone
    from zoneinfo import ZoneInfo

    def at(h, m):
        return datetime(2026, 11, 12, h, m, tzinfo=ZoneInfo(tz)).timestamp()

    assert hub.alerts._quiet_now(q, at(23, 0)) and hub.alerts._quiet_now(q, at(5, 0))
    assert not hub.alerts._quiet_now(q, at(12, 0)) and not hub.alerts._quiet_now({**q, "on": False}, at(23, 0))


def test_payload_silent_and_picture():
    from hub.push import alert_payload

    p = alert_payload("t", "b", cat="cameras", silent=True, image="/v1/x.jpg")
    assert "sound" not in p["aps"] and p["aps"]["interruption-level"] == "passive"
    assert p["aps"]["mutable-content"] == 1 and p["image"] == "/v1/x.jpg"


def test_test_alert_reports_apples_answer(hub, people, home_client):
    from hub.push import PushResult

    sent = []

    async def fake_send(token, payload, **kw):
        sent.append((token, payload, kw))
        return PushResult(False, 400, "BadDeviceToken")

    hub.vault.set("apns.auth_key_p8", "k")
    hub.vault.set("apns.key_id", "ABC")
    hub.config.apns.team_id = "T"
    hub.apns.send = fake_send  # type: ignore[method-assign]
    ph = people["phones"]["Dylan"]
    hub.db.update("devices", "id", ph.device_id, {"apns_token": "ab12", "apns_env": "sandbox"})
    r = home_client.post("/v1/alerts/test", headers=ph.h).json()
    assert r["ok"] is False and r["reason"] == "BadDeviceToken" and r["env"] == "sandbox"
    assert sent[0][2]["env"] == "sandbox"


async def test_not_a_person_teaches_frigate_and_mutes_that_spot(hub, people, sent, home_client):
    box = [0.5, 0.5, 0.1, 0.3]
    e = {**ev(eid="evt1"), "data": {"box": box}}
    plus = []

    async def event(eid):
        return e

    async def fp(eid):
        plus.append(eid)

    hub.frigate.event = event  # type: ignore[method-assign]
    hub.frigate.mark_false_positive = fp  # type: ignore[method-assign]
    assert await hub.alerts.on_frigate(e) == 2
    r = home_client.post("/v1/cameras/events/evt1/wrong", headers=people["phones"]["Avery"].h).json()
    assert r["frigate_plus"] is True and plus == ["evt1"] and r["ignored_spot"]["camera"] == "140_frontdoor"
    # Same spot again: no alert. Somewhere else on the same camera: alert.
    later = time.time() + 999
    assert await hub.alerts.on_frigate({**ev(eid="evt2"), "data": {"box": [0.51, 0.49, 0.1, 0.3]}}, now=later) == 0
    assert await hub.alerts.on_frigate({**ev(eid="evt3"), "data": {"box": [0.05, 0.1, 0.1, 0.3]}}, now=later) == 2
    # The earlier alert left the inbox
    assert hub.db.one("SELECT COUNT(*) n FROM inbox WHERE acked_at IS NULL AND title LIKE 'Person at%' "
                      "AND person_id=?", [people["avery"]["id"]])["n"] == 1
    items = home_client.get("/v1/cameras/ignored", headers=people["phones"]["Dylan"].h).json()["items"]
    assert len(items) == 1
    assert home_client.delete(f"/v1/cameras/ignored/{items[0]['id']}", headers=people["phones"]["Dylan"].h).status_code == 200
    assert home_client.post("/v1/cameras/events/evt1/wrong", headers=people["phones"]["Theo"].h).status_code == 403
    assert sent[0]["extra"] == {"event_id": e["id"], "label": "person"} and sent[0]["push_category"] == "CAMERA_PERSON"


async def test_firewalla_alarms_new_device_and_kids(hub, people, sent):
    hub.db.insert("network_rules", {"rule_id": "r1", "person_id": people["theo"]["id"], "mac": "AA:BB:CC:00:11:22",
                                    "device_name": "Theo's iPad", "created_at": time.time()})
    noon = datetime(2026, 11, 12, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).timestamp()
    night = noon + 11 * 3600  # 11 pm
    # New device: Dylan only (admin)
    n = await hub.alerts.on_firewalla({"aid": 1, "type": 5, "ts": noon, "message": "A new device joined",
                                       "device": {"name": "Pixel 9", "mac": "11:22:33:44:55:66"}}, now=noon)
    assert n == 1 and who(hub, sent) == ["Dylan"] and sent[0]["cat"] == "systems"
    sent.clear()
    # Adult site on Theo's iPad: both parents
    n = await hub.alerts.on_firewalla({"aid": 2, "type": 10, "ts": noon, "message": "porn activity",
                                       "device": {"name": "iPad", "mac": "aa:bb:cc:00:11:22"}}, now=noon)
    assert n == 2 and who(hub, sent) == ["Avery", "Dylan"] and "Theo" in sent[0]["title"]
    sent.clear()
    # Gaming in the afternoon is off by default; at 11 pm it's a bedtime alert
    assert await hub.alerts.on_firewalla({"aid": 3, "type": 9, "ts": noon, "device": {"mac": "AA:BB:CC:00:11:22", "name": "iPad"}}, now=noon) == 0
    n = await hub.alerts.on_firewalla({"aid": 4, "type": 9, "ts": night, "device": {"mac": "AA:BB:CC:00:11:22", "name": "iPad"}}, now=night)
    assert n == 2 and "bedtime" in sent[0]["title"]
    # A device in a Firewalla group named after the child counts too
    assert hub.alerts.kid_for_device({"name": "Switch", "mac": "X", "group": {"name": "Theo"}})["name"] == "Theo"


async def test_firewalla_poll_starts_from_now(hub, people, sent):
    rows = [{"aid": 9, "type": 5, "ts": 100.0, "device": {"name": "Old"}}]

    async def alarms(limit=50):
        return rows

    hub.config.firewalla.enabled = True
    hub.config.firewalla.msp_domain = "x.firewalla.net"
    hub.firewalla.alarms = alarms  # type: ignore[method-assign]
    assert await hub.alerts.poll_firewalla() == 0          # first look: no backlog
    rows.append({"aid": 10, "type": 5, "ts": 200.0, "device": {"name": "New"}})
    assert await hub.alerts.poll_firewalla() == 1
    assert await hub.alerts.poll_firewalla() == 0


async def test_media_ready_is_pushed_without_opening_the_app(hub, people, sent):
    from hub.db import new_id

    rid = new_id("req")
    hub.db.insert("requests", {"id": rid, "created_at": time.time(), "kind": "media", "requester_id": people["theo"]["id"],
                               "title": "Bluey", "payload": {}, "status": "approved", "external_id": "77"})

    async def status(ext):
        return {"media": {"status": 5}}

    hub.config.jellyseerr.enabled = True
    hub.config.jellyseerr.url = "http://js.test"
    hub.jellyseerr.request_status = status  # type: ignore[method-assign]
    await hub.alerts.poll_media()
    assert hub.db.one("SELECT status FROM requests WHERE id=?", [rid])["status"] == "ready"
    assert [s["title"] for s in sent] == ["Bluey is ready to watch"] and who(hub, sent) == ["Theo"]


async def test_history_keeps_alerts_whether_pushed_or_not(hub, people, home_client):
    hub.frigate._info = {"140_frontdoor": {"enabled": True}}
    # Nobody has alerts on: nothing is pushed, but the history still has it, once per minute per camera.
    t = time.time()
    assert await hub.alerts.on_frigate(ev(eid="h1"), now=t) == 0
    assert await hub.alerts.on_frigate(ev(eid="h2"), now=t + 5) == 0
    assert await hub.alerts.on_firewalla({"aid": 7, "type": 5, "ts": t, "device": {"name": "Pixel", "mac": "1"}}, now=t) == 0
    dylan, avery, theo = (people["phones"][n].h for n in ("Dylan", "Avery", "Theo"))
    d = home_client.get("/v1/alerts/history", headers=dylan).json()
    assert [i["kind"] for i in d["items"]] == ["network", "camera"]
    assert d["items"][1]["payload"]["event_id"] == "h1" and "network" in [k["id"] for k in d["kinds"]]
    a = home_client.get("/v1/alerts/history", headers=avery).json()
    assert [i["kind"] for i in a["items"]] == ["camera"]           # network is Dylan's
    assert home_client.get("/v1/alerts/history?kind=network", headers=avery).json()["items"] == []
    assert home_client.get("/v1/alerts/history", headers=theo).json()["items"] == []  # no cameras for kids
    assert [i["kind"] for i in home_client.get("/v1/alerts/history?kind=camera", headers=dylan).json()["items"]] == ["camera"]
