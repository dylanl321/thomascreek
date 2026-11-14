"""Door alerts for the TV: discovered on their own, quiet at night, picture and stream stay on the hub."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hub.home_alerts import rewrite_playlist
from hub.routes.home import shape_event

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token


def _door(hub):
    hub.home.states["binary_sensor.front_doorbell"] = {
        "entity_id": "binary_sensor.front_doorbell", "state": "off",
        "attributes": {"friendly_name": "Front doorbell", "device_class": "occupancy"},
    }
    hub.home.states["camera.kitchen"] = {
        "entity_id": "camera.kitchen", "state": "idle",
        "attributes": {"friendly_name": "Kitchen"},
    }
    hub.home.entity_area["camera.kitchen"] = "kitchen"


def _night() -> float:
    return datetime(2026, 11, 14, 23, 15, tzinfo=ZoneInfo("America/New_York")).timestamp()


def _afternoon() -> float:
    return datetime(2026, 11, 14, 15, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_a_doorbell_reaches_the_tv_and_the_picture_stays_on_the_hub(hub, home_client, people):
    _door(hub)
    dylan = people["phones"]["Dylan"]
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    clock = Clock()
    tv_token = _token(home_client, key, clock)["access_token"]
    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    tv_subject = hub.tv_app.session(TV_ID, None, hub.tokens.verify(tv_token)["key"])[0]
    phones = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id)
    tvs = hub.bus.subscribe(lambda ev: shape_event(hub, tv_subject, ev), None, device_id=f"tv:{TV_ID}", tv=True)

    alert = hub.home_alerts.note_doorbell("binary_sensor.front_doorbell", now=_afternoon())
    assert alert["kind"] == "doorbell" and alert["title"] == "Someone's at the door"
    assert alert["camera"] == "driveway"
    assert alert["snapshot_url"].startswith("/v1/tv/alerts/") and alert["stream_url"].endswith("/stream")
    assert alert["ttl_s"] == 90 and alert["stream"] == "mjpeg"
    assert "ha.test" not in str(alert) and "token" not in str(alert)
    assert phones.queue.get_nowait()["type"] == "home_alert"
    assert tvs.queue.get_nowait()["data"]["id"] == alert["id"]
    assert hub.home_alerts.note_doorbell("binary_sensor.front_doorbell", now=_afternoon()) is None

    async def proxy(_entity):
        return b"\xff\xd8picture"

    hub.ha.camera_proxy = proxy
    shot = home_client.get(alert["snapshot_url"], headers=_auth(tv_token))
    assert shot.status_code == 200 and shot.content == b"\xff\xd8picture"
    assert shot.headers["content-type"] == "image/jpeg"

    class Upstream:
        headers = {"content-type": "multipart/x-mixed-replace; boundary=frame"}

        async def aiter_raw(self):
            yield b"--frame"

        async def aclose(self):
            return None

    async def camera_stream(_entity):
        return Upstream()

    hub.ha.camera_stream = camera_stream
    played = home_client.get(alert["stream_url"], headers=_auth(tv_token))
    assert played.status_code == 200 and played.content == b"--frame"
    assert "multipart" in played.headers["content-type"]
    assert home_client.get("/v1/tv/alerts/ha_deadbeef/snapshot.jpg", headers=dylan.h).status_code == 404


def test_person_at_the_door_is_quiet_overnight_and_motion_stays_off(hub, people):
    _door(hub)
    day = hub.home_alerts.note_frigate({"id": "e1", "camera": "driveway", "label": "person"}, now=_afternoon())
    assert day["kind"] == "person" and day["camera"] == "driveway" and "Someone at the driveway" in day["title"]
    assert hub.home_alerts.note_frigate({"id": "e2", "camera": "kitchen", "label": "person"}, now=_afternoon()) is None
    assert hub.home_alerts.note_frigate({"id": "e3", "camera": "driveway", "label": "motion"}, now=_afternoon()) is None
    assert hub.home_alerts.note_frigate({"id": "e4", "camera": "driveway", "label": "person"}, now=_night()) is None
    bell = hub.home_alerts.note_doorbell("binary_sensor.front_doorbell", now=_night())
    assert bell["kind"] == "doorbell"

    dylan = people["phones"]["Dylan"]
    saved = dylan.client.put("/v1/tv/alerts/settings", headers=dylan.h, json={
        "cameras": ["kitchen"], "motion": True, "quiet": {"from": "00:00", "to": "00:00"},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["auto"] is False and saved.json()["cameras"] == ["kitchen"] and saved.json()["motion"] is True
    moved = hub.home_alerts.note_frigate({"id": "e5", "camera": "kitchen", "label": "motion"}, now=_night())
    assert moved["kind"] == "motion" and moved["camera"] == "kitchen"
    assert hub.home_alerts.note_frigate({"id": "e6", "camera": "driveway", "label": "person"}, now=_night()) is None
    assert people["phones"]["Avery"].client.put("/v1/tv/alerts/settings", headers=people["phones"]["Avery"].h,
                                                json={"motion": True}).status_code == 403


def test_the_playlist_points_back_at_the_hub():
    text = rewrite_playlist("#EXTM3U\n#EXTINF:2,\nseg.ts?n=1\nhttp://go2rtc.local/secret.ts\n../etc/passwd\n", "ha_abc123")
    assert "seg.ts" in text and text.count("/v1/tv/alerts/ha_abc123/media?src=") == 1
    assert "go2rtc" not in text and "passwd" not in text and "secret" not in text


def test_a_phone_away_from_home_does_not_get_the_picture(hub, home_client, away_client, people):
    _door(hub)
    alert = hub.home_alerts.note_doorbell("binary_sensor.front_doorbell", now=_afternoon())

    async def proxy(_entity):
        return b"\xff\xd8picture"

    hub.ha.camera_proxy = proxy
    theo = people["phones"]["Theo"]
    assert away_client.get(alert["snapshot_url"], headers=theo.h).status_code == 403
    assert home_client.get(alert["snapshot_url"], headers=theo.h).status_code == 200
