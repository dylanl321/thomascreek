"""A TV updates itself overnight and tells only the admin how it went."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hub.routes.home import shape_event

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token

INSTALL = (
    "Download the APK with this TV's bearer, check sha256 and signer_sha256, "
    "then hand the file to PackageInstaller. The person confirms on the TV."
)
TZ = ZoneInfo("America/New_York")


def _build(hub) -> None:
    hub.tv_app.dir.mkdir(parents=True, exist_ok=True)
    hub.tv_app.apk_path(602).write_bytes(b"apk")
    hub.tv_app.db.kv_set("tvapp.latest", {
        "versionCode": 602, "versionName": "1.4.1-tc.2", "sha256": "abc", "notes": "nightly",
    })
    hub.tv_app.db.kv_set("tvapp.signer", "deadbeef")


def _tv(hub, home_client) -> dict[str, str]:
    _build(hub)
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    token = _token(home_client, key, Clock())["access_token"]
    return _auth(token)


def test_an_idle_tv_is_told_the_window_and_skips_a_download_it_already_has(hub, home_client, away_client, people):
    headers = _tv(hub, home_client)
    manifest = home_client.get("/v1/tv/update", headers=headers)
    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["version_code"] == 602 and body["sha256"] == "abc" and body["signer_sha256"] == "deadbeef"
    assert body["apk_path"] == "/v1/tv/apk" and body["install"] == INSTALL
    assert body["idle_only"] is True and body["silent"] is True
    assert body["window"] == {"from": "01:00", "to": "06:00", "timezone": "America/New_York"}
    assert hub.tv_app.in_update_window(datetime(2026, 11, 14, 1, 0, tzinfo=TZ))
    assert not hub.tv_app.in_update_window(datetime(2026, 11, 14, 6, 0, tzinfo=TZ))
    assert not hub.tv_app.in_update_window(datetime(2026, 11, 14, 21, 30, tzinfo=TZ))

    current = home_client.get("/v1/tv/update", headers=headers, params={"current": "602"})
    assert current.json() == {"version_code": 602, "up_to_date": True}
    newer = home_client.get("/v1/tv/update", headers=headers, params={"current": "700"})
    assert newer.json() == {"version_code": 602, "up_to_date": True}
    behind = home_client.get("/v1/tv/update", headers=headers, params={"current": "601"})
    assert behind.json()["apk_path"] == "/v1/tv/apk" and behind.json()["silent"] is True
    junk = home_client.get("/v1/tv/update", headers=headers, params={"current": "nope"})
    assert junk.json()["version_name"] == "1.4.1-tc.2"

    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/tv/update", headers=dylan.h).status_code == 403
    assert away_client.get("/v1/tv/update", headers=headers).status_code == 403


def test_the_report_stays_on_the_admin_view(hub, home_client, away_client, people):
    headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    phone = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id, subject.device_id)
    sent = home_client.post("/v1/tv/update/health", headers=headers, json={
        "version_code": 602, "boot_ok": True, "hub_ok": True, "playback_ok": False, "rollback": False,
    })
    assert sent.status_code == 200 and sent.json() == {"ok": True}
    assert phone.queue.empty()
    updates = dylan.client.get("/v1/tvapp", headers=dylan.h).json()["updates"]
    assert updates[0]["id"] == TV_ID and updates[0]["name"] == "Living room"
    assert updates[0]["playback_ok"] is False and updates[0]["ok"] is False and updates[0]["rollback"] is False

    again = home_client.post("/v1/tv/update/health", headers=headers, json={
        "version_code": 601, "boot_ok": True, "hub_ok": True, "playback_ok": True, "rollback": True,
    })
    assert again.json() == {"ok": True}
    rolled = dylan.client.get("/v1/tvapp", headers=dylan.h).json()["updates"]
    assert len(rolled) == 1 and rolled[0]["rollback"] is True and rolled[0]["ok"] is False
    assert rolled[0]["version_code"] == 601

    bad = home_client.post("/v1/tv/update/health", headers=headers, json={"version_code": 602, "boot_ok": "yes"})
    assert bad.status_code == 400 and bad.json()["message"] == "Say how the update went."
    assert dylan.client.post("/v1/tv/update/health", headers=dylan.h, json={
        "version_code": 602, "boot_ok": True, "hub_ok": True, "playback_ok": True, "rollback": False,
    }).status_code == 403
    assert away_client.post("/v1/tv/update/health", headers=headers, json={
        "version_code": 602, "boot_ok": True, "hub_ok": True, "playback_ok": True, "rollback": False,
    }).status_code == 403
    assert people["phones"]["Avery"].client.get("/v1/tvapp", headers=people["phones"]["Avery"].h).status_code == 403
