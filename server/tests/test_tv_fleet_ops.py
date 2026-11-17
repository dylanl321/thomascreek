"""A new build goes to one room first, and a root box can run one allowlisted command."""

from __future__ import annotations

import hashlib

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token
from tests.test_tv_downloads import _sign_in

OTHER = "tv_den_0123456789abcd"
REPORT = {
    "capability": "owner", "model": "onn. 4K Streaming Box", "android": "12", "platform": "google_tv",
    "app_version": "1.5.0", "cpu_pct": 12,
    "packages": {"hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "changed": True, "list": []},
}


def _pair(hub, home_client, install_id, name):
    key = Key()
    _enroll(home_client, key, install_id, name)
    hub.tv_app.set_tv(install_id, status="approved", room="living_room")
    token = _token(home_client, key, Clock(), install_id)["access_token"]
    return _auth(token)


def _builds(hub):
    hub.tv_app.dir.mkdir(parents=True, exist_ok=True)
    hub.tv_app.apk_path(601).write_bytes(b"old")
    hub.tv_app.apk_path(602).write_bytes(b"new")
    hub.db.kv_set("tvapp.latest", {"versionCode": 602, "versionName": "1.5.0", "sha256": hashlib.sha256(b"new").hexdigest()})
    hub.db.kv_set("tvapp.signer", "deadbeef")


def _health(ok: bool) -> dict:
    return {"version_code": 602, "boot_ok": True, "hub_ok": True, "playback_ok": ok, "rollback": False}


def test_a_rollout_waits_for_the_first_room(hub, people, home_client):
    _builds(hub)
    first = _pair(hub, home_client, TV_ID, "Living room")
    second = _pair(hub, home_client, OTHER, "Den")
    dylan = people["phones"]["Dylan"]
    started = dylan.client.post("/v1/tv/fleet/rollout", headers=dylan.h, json={
        "version_code": 602, "stage": [TV_ID], "then": "all", "now": False,
    })
    assert started.status_code == 200, started.text
    assert started.json()["rollout"]["state"] == "staging" and started.json()["rollout"]["rest"] == "held"
    assert home_client.get("/v1/tv/update", headers=first).json()["version_code"] == 602
    held = home_client.get("/v1/tv/update", headers=second).json()
    assert held["version_code"] == 601 and held["apk_path"] == "/v1/tv/apk?version_code=601"
    assert dylan.client.post("/v1/tv/fleet/rollout", headers=dylan.h, json={"version_code": 999, "stage": [TV_ID]}).json()["message"] == "That build isn't on the hub."

    home_client.post("/v1/tv/update/health", headers=first, json=_health(True))
    assert dylan.client.get("/v1/tv/fleet/rollout", headers=dylan.h).json()["rollout"]["state"] == "rolling"
    assert home_client.get("/v1/tv/update", headers=second).json()["version_code"] == 602

    home_client.post("/v1/tv/update/health", headers=second, json=_health(False))
    assert dylan.client.get("/v1/tv/fleet/rollout", headers=dylan.h).json()["rollout"]["state"] == "rolling"
    dylan.client.post("/v1/tv/fleet/rollout", headers=dylan.h, json={"version_code": 602, "stage": [TV_ID], "then": "all"})
    home_client.post("/v1/tv/update/health", headers=first, json=_health(False))
    stopped = dylan.client.get("/v1/tv/fleet/rollout", headers=dylan.h).json()["rollout"]
    assert stopped["state"] == "stopped" and stopped["rest"] == "held"
    assert home_client.get("/v1/tv/update", headers=second).json()["version_code"] == 601


def test_shell_is_allowlisted_and_stays_off_the_page_until_asked(hub, people, home_client, away_client):
    headers = _pair(hub, home_client, TV_ID, "Living room")
    home_client.post("/v1/tv/fleet/report", headers=headers, json=REPORT)
    dylan = people["phones"]["Dylan"]
    missing = dylan.client.post(f"/v1/tv/fleet/{TV_ID}/shell", headers=dylan.h, json={"template": "logcat"})
    assert missing.json()["message"] == "This TV has no shell."
    rooted = dict(REPORT)
    rooted["capability"] = "root"
    home_client.post("/v1/tv/fleet/report", headers=headers, json=rooted)
    sent = dylan.client.post(f"/v1/tv/fleet/{TV_ID}/shell", headers=dylan.h, json={"template": "logcat"})
    assert sent.status_code == 200, sent.text
    assert sent.json()["ok"] is True
    refused = dylan.client.post(f"/v1/tv/fleet/{TV_ID}/shell", headers=dylan.h, json={"command": "id"})
    assert refused.json() == {"ok": False, "message": "Shell commands are off."}
    assert away_client.post(f"/v1/tv/fleet/{TV_ID}/shell", headers=dylan.h, json={"template": "uptime"}).status_code == 403
    listed = dylan.client.get("/v1/tv/fleet/audit", headers=dylan.h).json()
    assert listed["items"][0]["template"] == "logcat" and listed["items"][0]["command"] is None
    home_client.post("/v1/tv/manage/ack", headers=headers, json={"id": sent.json()["id"], "ok": True, "output": "quiet"})
    detail = dylan.client.get(f"/v1/tv/fleet/{TV_ID}/commands/{sent.json()['id']}", headers=dylan.h).json()
    assert detail["output"] == "quiet"
    assert dylan.client.get("/v1/tv/fleet/audit", headers=dylan.h).json()["items"][0]["ok"] is True

    csrf = _sign_in(home_client, people)
    listed = home_client.get("/admin/devices").text
    assert "Living room" in listed and "No TVs have checked in yet." not in listed
    page = home_client.get(f"/admin/devices/{TV_ID}?tab=remote").text
    assert "Reboot" in page and "logcat" in page and "One command" not in page
    hub.config.tv_fleet.shell = True
    assert "One command" in home_client.get(f"/admin/devices/{TV_ID}?tab=remote").text
    asked = home_client.post(f"/admin/devices/{TV_ID}/command", data={"csrf": csrf, "action": "reboot"}, follow_redirects=False)
    assert asked.status_code == 303 and "Asked" in asked.headers["location"]
    setup = home_client.get("/admin/devices/provision").text
    assert "adb shell dpm set-device-owner me.dlewis.thomascreek.tv/.DeviceAdminReceiver" in setup
    assert "Certified Google TV" in setup
    _builds(hub)
    quiet = home_client.post("/admin/devices/provision", data={"csrf": csrf, "ssid": "House", "password": "not-saved"}, follow_redirects=True)
    assert quiet.status_code == 200 and "not-saved" not in quiet.text and "The Wi-Fi is in the QR only." in quiet.text


def test_a_held_tv_downloads_the_pinned_build(hub, home_client):
    _builds(hub)
    first = _pair(hub, home_client, TV_ID, "Living room")
    pinned = home_client.get("/v1/tv/apk", headers=first, params={"version_code": 601})
    assert pinned.status_code == 200 and pinned.content == b"old"
    assert home_client.get("/v1/tv/apk", headers=first).content == b"new"
    assert home_client.get("/v1/tv/apk", headers=first, params={"version_code": 777}).status_code == 404
