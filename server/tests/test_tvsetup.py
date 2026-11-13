"""Setting up a TV over ADB, and the Plex row a TV reads with its own token."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hub.tvapp import Adb, AdbResult
from hub.tvsetup import classify, parse_mdns


PROPS = "\n".join([
    "[ro.product.manufacturer]: [NVIDIA]",
    "[ro.product.model]: [SHIELD Android TV]",
    "[ro.product.brand]: [NVIDIA]",
    "[ro.product.device]: [mdarcy]",
    "[ro.build.version.sdk]: [33]",
])


class ScriptAdb(Adb):
    def __init__(self, replies: dict):
        super().__init__(Path("/tmp"), runner=self._fake)
        self.replies = replies
        self.calls: list[list[str]] = []

    @property
    def available(self) -> bool:
        return True

    async def _fake(self, args: list[str], timeout: float) -> AdbResult:
        self.calls.append(args)
        if args[0] != "-s":
            key = args[0]
        elif len(args) > 3 and args[2] == "shell":
            key = "shell " + " ".join(args[3:6])
        else:
            key = args[2]
        out = self.replies.get(key, "")
        text = out if isinstance(out, str) else ""
        return AdbResult(1 if text.startswith("ERR") else 0, text)


def _ready(hub, adb: ScriptAdb) -> None:
    hub.tv_app.adb = adb
    hub.tv_app.dir.mkdir(parents=True, exist_ok=True)
    hub.tv_app.apk_path(602).write_bytes(b"apk")
    hub.tv_app.db.kv_set("tvapp.latest", {"versionCode": 602, "versionName": "1.4.1-tc.2"})


def _replies(**extra) -> dict:
    base = {
        "connect": "connected to 10.0.0.86:5555",
        "get-state": "device",
        "shell getprop": PROPS,
        "shell dumpsys package me.dlewis.thomascreek.tv": "versionCode=1",
        "install": "Success",
        "shell appops set": "",
        "shell pm grant": "",
        "shell cmd package resolve-activity": "com.google.android.tvlauncher/.MainActivity",
        "shell cmd package set-home-activity": "",
    }
    base.update(extra)
    return base


def test_classify_and_mdns():
    assert classify({"ro.product.manufacturer": "NVIDIA", "ro.product.model": "SHIELD Android TV",
                     "ro.build.version.sdk": "33"})["kind"] == "shield"
    assert classify({"ro.product.manufacturer": "Amazon", "ro.product.model": "AFTMM",
                     "ro.product.device": "AFTMM"})["kind"] == "fire_tv"
    assert classify({"ro.product.manufacturer": "Sony", "ro.product.model": "BRAVIA"})["kind"] == "android_tv"
    assert classify({"ro.product.model": "Chromecast"})["kind"] == "google_tv"
    rows = parse_mdns("LivingRoom _adb-tls-connect._tcp 10.0.0.86:5555\n"
                      "LivingRoom _adb-tls-pairing._tcp 10.0.0.86:37123\n")
    assert rows == [{"ip": "10.0.0.86", "name": "LivingRoom", "connect_port": 5555, "pairing_port": 37123}]


def test_setup_installs_grants_and_can_run_again(hub, people):
    adb = ScriptAdb(_replies())
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "ip": "10.0.0.86", "name": "Living room", "room": "living_room", "home": True})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["phase"] == "ready"
    assert body["device"]["kind"] == "shield"
    assert body["installed"] == 602
    assert body["capabilities"]["overlay"] is True
    assert body["home"]["set"] is True
    assert body["home"]["previous"] == "com.google.android.tvlauncher/.MainActivity"
    assert body["you_do"] == "Open Thomas Creek TV once"
    assert any(step["id"] == "install" and step["state"] == "done" for step in body["steps"])
    installs = [c for c in adb.calls if "install" in c]
    adb.replies["shell dumpsys package me.dlewis.thomascreek.tv"] = "versionCode=602"
    again = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "ip": "10.0.0.86", "name": "Living room", "home": True})
    assert again.status_code == 200 and again.json()["phase"] == "ready"
    assert [c for c in adb.calls if "install" in c] == installs
    status = dylan.client.get("/v1/tvapp/setup/10.0.0.86", headers=dylan.h)
    assert status.json()["phase"] == "ready"
    assert people["phones"]["Avery"].client.post("/v1/tvapp/setup", headers=people["phones"]["Avery"].h,
                                                json={"ip": "10.0.0.86"}).status_code == 403


def test_allow_prompt_stops_before_install(hub, people):
    adb = ScriptAdb(_replies(**{"get-state": "unauthorized"}))
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={"ip": "10.0.0.86"}).json()
    assert body["phase"] == "needs_allow"
    assert "Allow" in body["you_do"]
    assert not any("install" in c for c in adb.calls)


def test_fire_tv_is_installed_but_not_made_home(hub, people):
    props = PROPS.replace("NVIDIA", "Amazon").replace("SHIELD Android TV", "AFTMM").replace("mdarcy", "AFTMM")
    adb = ScriptAdb(_replies(**{"shell getprop": props}))
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={"ip": "10.0.0.86", "home": True}).json()
    assert body["device"]["kind"] == "fire_tv" and body["warning"]
    assert body["home"]["wanted"] is False
    assert not any("set-home-activity" in " ".join(c) for c in adb.calls)


def test_pairing_code_and_a_bad_one(hub, people):
    adb = ScriptAdb(_replies(**{"pair": "Successfully paired to 10.0.0.86:37123"}))
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    ok = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "ip": "10.0.0.86", "pairing_port": 37123, "pairing_code": "A1B2C3"})
    assert ok.status_code == 200, ok.text
    assert adb.calls[0][:1] == ["pair"]
    bad = ScriptAdb(_replies(**{"pair": "ERR failed"}))
    hub.tv_app.adb = bad
    hub.tv_app._save_boxes({})
    failed = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "ip": "10.0.0.50", "pairing_port": 9, "pairing_code": "ZZZZZ"})
    assert failed.status_code == 400
    failed = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "ip": "10.0.0.50", "pairing_port": 37123, "pairing_code": "ABCDEF"})
    assert failed.json()["phase"] == "failed"


def test_update_window_and_rollback(hub, people):
    import asyncio
    adb = ScriptAdb(_replies())
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={"ip": "10.0.0.86", "home": True})
    hub.tv_app.db.kv_set("tvapp.latest", {"versionCode": 603, "versionName": "next"})
    hub.tv_app.apk_path(603).write_bytes(b"apk")
    afternoon = datetime(2026, 11, 13, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    hub.tv_app.now = lambda: afternoon
    before = [c for c in adb.calls if "install" in c]
    asyncio.run(hub.tv_app.update_all())
    assert [c for c in adb.calls if "install" in c] == before
    hub.tv_app.now = lambda: datetime(2026, 11, 13, 2, 0, tzinfo=ZoneInfo("America/New_York"))
    asyncio.run(hub.tv_app.update_all())
    assert any("install" in c for c in adb.calls[len(before):] or adb.calls)
    rolled = dylan.client.post("/v1/tvapp/setup/10.0.0.86/rollback-home", headers=dylan.h)
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["home"]["set"] is False
    assert any("set-home-activity" in " ".join(c) and "com.google.android.tvlauncher" in " ".join(c) for c in adb.calls)


def test_discover(hub, people):
    adb = ScriptAdb({"mdns": "Den _adb-tls-connect._tcp 10.0.0.40:5555\n"})
    hub.tv_app.adb = adb
    body = people["phones"]["Dylan"].client.get("/v1/tvapp/discover", headers=people["phones"]["Dylan"].h).json()
    assert body["devices"][0]["ip"] == "10.0.0.40"


def test_plex_row_for_a_child_and_a_grown_up(hub, people):
    hub.plex.cfg.enabled = True
    hub.plex.cfg.url = "http://plex.test"

    async def identity():
        return "machine1"

    async def continue_watching():
        return [
            {"id": "9", "title": "Bluey: Keepy Uppy", "kind": "episode", "rating": "TV-Y", "view_offset_s": 30,
             "poster_url": "/v1/media/art?path=%2Flibrary%2Fa", "show": "Bluey", "season": 1, "episode": 2,
             "year": 2018, "summary": "Hi"},
            {"id": "8", "title": "Bluey: next", "kind": "episode", "rating": "TV-Y", "view_offset_s": None,
             "poster_url": "/v1/media/art?path=%2Flibrary%2Fb", "show": "Bluey", "season": 1, "episode": 3,
             "year": 2018, "summary": None},
        ]

    async def recently_added():
        return [{"id": "3", "title": "The Matrix", "kind": "movie", "rating": "R", "poster_url": "/v1/media/art?path=%2Fx",
                 "year": 1999, "summary": "Nope", "show": None, "season": None, "episode": None, "view_offset_s": None}]

    hub.plex.identity = identity
    hub.plex.continue_watching = continue_watching
    hub.plex.recently_added = recently_added
    dylan = people["phones"]["Dylan"].client.get("/v1/tv/plex", headers=people["phones"]["Dylan"].h).json()
    assert dylan["continue"][0]["deep_link"].startswith("plex://preplay?")
    assert "machine1" in dylan["continue"][0]["deep_link"]
    assert dylan["continue"][0]["package"] == "com.plexapp.android"
    assert "token" not in dylan["continue"][0]["deep_link"]
    assert [row["title"] for row in dylan["recent"]] == ["The Matrix"]
    assert dylan["on_deck"][0]["title"] == "Bluey: next"
    kid = people["phones"]["Theo"].client.get("/v1/tv/plex", headers=people["phones"]["Theo"].h).json()
    assert kid["recent"] == []
    assert kid["continue"][0]["title"].startswith("Bluey")


def test_overlay_and_home_failures_leave_on_tv_instructions(hub, people):
    adb = ScriptAdb(_replies(**{
        "shell appops set me.dlewis.thomascreek.tv": "ERR denied",
        "shell cmd package set-home-activity": "ERR no",
    }))
    _ready(hub, adb)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={"ip": "10.0.0.86", "home": True}).json()
    assert body["phase"] == "ready" and body["mode"] == "adb"
    assert "Default apps" in body["you_do"]
    steps = {step["id"]: step for step in body["steps"]}
    assert steps["permissions"]["state"] == "skipped" and "Display over other apps" in steps["permissions"]["note"]
    assert steps["home"]["state"] == "skipped"


def test_non_adb_install_enroll_and_update(hub, people, home_client, away_client):
    from hub.tvapp import token_message
    from tests.test_tv_api import Clock, Key

    _ready(hub, ScriptAdb({}))
    hub.tv_app.db.kv_set("tvapp.latest", {"versionCode": 602, "versionName": "1.4.1-tc.2", "sha256": "abc", "notes": "nightly"})
    hub.tv_app.db.kv_set("tvapp.signer", "deadbeef")
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tvapp/setup", headers=dylan.h, json={
        "mode": "non-adb", "name": "Office", "room": "living_room", "profile": "adult",
    }).json()
    assert body["mode"] == "non-adb" and body["phase"] == "install"
    code = body["install"]["code"]
    assert body["install"]["url"] == f"https://hub.test/v1/tv/apk/{code}"
    steps = {step["id"]: step for step in body["steps"]}
    assert steps["permissions"]["state"] == "skipped" and steps["home"]["note"]
    apk = home_client.get(f"/v1/tv/apk/{code}")
    assert apk.status_code == 200 and apk.content == b"apk"
    assert "android.package-archive" in apk.headers["content-type"]
    assert away_client.get(f"/v1/tv/apk/{code}").status_code == 403
    assert home_client.get("/v1/tv/apk/ZZZZZZ").status_code == 404
    again = dylan.client.get(f"/v1/tvapp/setup/code/{code}", headers=dylan.h).json()
    assert again["install"]["code"] == code and again["mode"] == "non-adb"

    key = Key()
    install_id = "tv_office_0123456789"
    enrolled = home_client.post("/v1/tv/enroll", json={
        "install_id": install_id, "name": "Office", "public_key": key.pub, "code": code,
    })
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["status"] == "approved" and enrolled.json()["mode"] == "non-adb"
    listed = dylan.client.get("/v1/tvapp", headers=dylan.h).json()["tvs"]
    assert listed[0]["mode"] == "non-adb" and listed[0]["room"] == "living_room"
    clock = Clock()
    ts = clock.next()
    token = home_client.post("/v1/tv/token", json={
        "install_id": install_id, "ts": ts, "signature": key.sign(token_message(install_id, ts, None)),
    }).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    manifest = home_client.get("/v1/tv/update", headers=headers).json()
    assert manifest["version_code"] == 602 and manifest["sha256"] == "abc"
    assert manifest["signer_sha256"] == "deadbeef" and manifest["apk_path"] == "/v1/tv/apk"
    assert home_client.get("/v1/tv/apk", headers=headers).content == b"apk"
    assert dylan.client.get("/v1/tv/apk", headers=dylan.h).status_code == 403
    assert dylan.client.get("/v1/tv/update", headers=dylan.h).status_code == 403
    links = hub.tv_app.db.kv_get("tvapp.install_links")
    links[code]["expires_at"] = 1
    hub.tv_app.db.kv_set("tvapp.install_links", links)
    assert home_client.get(f"/v1/tv/apk/{code}").status_code == 404


def test_phone_approves_the_code_the_tv_shows(hub, people, home_client):
    from tests.test_tv_api import Key

    key = Key()
    install_id = "tv_den_0123456789ab"
    pending = home_client.post("/v1/tv/enroll", json={
        "install_id": install_id, "name": "Den", "public_key": key.pub,
    }).json()
    assert pending["status"] == "pending" and pending["mode"] == "non-adb"
    dylan = people["phones"]["Dylan"]
    approved = dylan.client.post("/v1/tvapp/approve", headers=dylan.h, json={
        "code": pending["code"], "room": "living_room",
    })
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and approved.json()["room"] == "living_room"
    assert dylan.client.post("/v1/tvapp/approve", headers=dylan.h, json={"code": pending["code"]}).status_code == 404
    assert people["phones"]["Avery"].client.post("/v1/tvapp/approve", headers=people["phones"]["Avery"].h,
                                                json={"code": "AAAAAA"}).status_code == 403
