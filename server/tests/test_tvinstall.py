"""The TV fleet list on the phone, and installing Thomas Creek TV on one or many TVs in the background."""

from __future__ import annotations

import time

from hub.remotes import Found
from hub.tvinstall import guide, kind_hint

from tests.test_tvsetup import ScriptAdb, _ready, _replies


def _wait(client, headers, ip, phases=("ready", "failed", "needs_allow", "offline"), seconds=5.0):
    end = time.time() + seconds
    body = {}
    while time.time() < end:
        body = client.get(f"/v1/tvapp/setup/{ip}", headers=headers).json()
        if body.get("phase") in phases:
            return body
        time.sleep(0.05)
    return body


def _scan(hub, *found):
    hub.remotes.scanner = lambda: list(found)


def test_guide_and_kind_from_the_scan():
    assert kind_hint("SHIELD", "SHIELD Android TV")["kind"] == "shield"
    assert kind_hint("Living Room", "Chromecast")["kind"] == "google_tv"
    assert "Network debugging" in " ".join(guide("shield", wireless=False))
    assert "pairing code" in " ".join(guide("google_tv", wireless=True))


def test_fleet_lists_every_android_tv_with_its_next_step(hub, people):
    adb = ScriptAdb(_replies(**{"mdns": "Bedroom _adb-tls-connect._tcp 10.0.2.114:41111\n"
                                        "Bedroom _adb-tls-pairing._tcp 10.0.2.114:37000\n"}))
    _ready(hub, adb)
    _scan(hub,
          Found("android", "10.0.0.86", "Living Room Shield", "SHIELD Android TV"),
          Found("android", "10.0.2.114", "Adeline's TV", "Hisense"),
          Found("apple", "10.0.3.143", "Living Room", "Apple TV 4K"))
    dylan = people["phones"]["Dylan"]
    page = dylan.client.get("/v1/tvapp/fleet", headers=dylan.h, params={"fresh": True})
    assert page.status_code == 200, page.text
    rows = {r["ip"]: r for r in page.json()["tvs"]}
    assert rows["10.0.0.86"]["state"] == "not_set_up"
    assert rows["10.0.0.86"]["kind"] == "shield"
    assert "Network debugging" in " ".join(rows["10.0.0.86"]["guide"])
    assert rows["10.0.2.114"]["state"] == "pairing" and rows["10.0.2.114"]["pairing_port"] == 37000
    assert rows["10.0.2.114"]["wireless"] is True
    assert page.json()["others"][0]["kind"] == "apple"
    avery = people["phones"]["Avery"]
    assert avery.client.get("/v1/tvapp/fleet", headers=avery.h).status_code == 403


def test_install_runs_in_the_background_on_many(hub, people):
    adb = ScriptAdb(_replies())
    _ready(hub, adb)
    _scan(hub, Found("android", "10.0.0.86", "Living Room Shield", "SHIELD Android TV"),
          Found("android", "10.0.0.11", "Master Bedroom", "onn 4K"))
    dylan = people["phones"]["Dylan"]
    res = dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": ["10.0.0.86", "10.0.0.11"]})
    assert res.status_code == 200, res.text
    assert len(res.json()["started"]) == 2 and not res.json()["problems"]
    assert _wait(dylan.client, dylan.h, "10.0.0.86")["phase"] == "ready"
    assert _wait(dylan.client, dylan.h, "10.0.0.11")["phase"] == "ready"
    assert hub.tv_app.boxes()["10.0.0.86"]["name"] == "Living Room Shield"
    rows = {r["ip"]: r for r in dylan.client.get("/v1/tvapp/fleet", headers=dylan.h).json()["tvs"]}
    assert rows["10.0.0.86"]["state"] == "ready" and rows["10.0.0.86"]["managed"] is True


def test_waits_for_allow_and_finishes_after_it(hub, people, monkeypatch):
    import hub.tvinstall as ti

    monkeypatch.setattr(ti, "RETRY_S", 0.05)
    adb = ScriptAdb(_replies(**{"get-state": "unauthorized"}))
    _ready(hub, adb)
    _scan(hub)
    sent = []

    async def note(title, body):
        sent.append(title)

    hub.tv_app.notify = note
    dylan = people["phones"]["Dylan"]
    dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": ["10.0.0.86"], "name": "Den"})
    assert _wait(dylan.client, dylan.h, "10.0.0.86", phases=("needs_allow",))["phase"] == "needs_allow"
    adb.replies["get-state"] = "device"  # someone tapped Allow
    assert _wait(dylan.client, dylan.h, "10.0.0.86", phases=("ready",))["phase"] == "ready"
    assert sent[0] == "Den needs one tap" and sent[-1] == "Den has Thomas Creek TV"


def test_wireless_debugging_pairs_and_uses_the_advertised_port(hub, people):
    adb = ScriptAdb(_replies(**{
        "pair": "Successfully paired to 10.0.2.114:37000",
        "mdns": "TV _adb-tls-connect._tcp 10.0.2.114:41111\nTV _adb-tls-pairing._tcp 10.0.2.114:37000\n",
    }))
    _ready(hub, adb)
    _scan(hub)
    dylan = people["phones"]["Dylan"]
    res = dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": ["10.0.2.114"], "pairing_code": "123456"})
    assert res.status_code == 200, res.text
    assert _wait(dylan.client, dylan.h, "10.0.2.114")["phase"] == "ready"
    assert ["pair", "10.0.2.114:37000", "123456"] in adb.calls
    assert ["connect", "10.0.2.114:41111"] in adb.calls
    assert hub.tv_app._serial("10.0.2.114") == "10.0.2.114:41111"


def test_offline_box_finds_its_new_wireless_port(hub):
    adb = ScriptAdb(_replies(**{"mdns": "TV _adb-tls-connect._tcp 10.0.2.114:42222\n"}))
    _ready(hub, adb)
    hub.tv_app._save_boxes({"10.0.2.114": {"name": "TV", "connect_port": 41111, "wireless": True}})

    async def connect(serial):
        return "ok" if serial.endswith(":42222") else "offline"

    adb.connect = connect
    import asyncio

    box = asyncio.run(hub.tv_app.refresh_box("10.0.2.114"))
    assert box["state"] == "ok" and box["connect_port"] == 42222


def test_install_needs_a_build_and_a_pick(hub, people):
    dylan = people["phones"]["Dylan"]
    hub.tv_app.adb = ScriptAdb(_replies())
    assert dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": []}).status_code == 400
    res = dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": ["10.0.0.86"]})
    assert res.status_code == 409 and res.json()["error"] == "no_build"


def test_install_downloads_the_build_when_the_token_is_newer(hub, people):
    adb = ScriptAdb(_replies())
    hub.tv_app.adb = adb
    hub.vault.set("github.tv_app_token", "tok")
    fetched = []

    async def check():
        fetched.append(1)
        _ready(hub, adb)
        return hub.tv_app.latest()

    hub.tv_app.check = check
    _scan(hub)
    dylan = people["phones"]["Dylan"]
    res = dylan.client.post("/v1/tvapp/install", headers=dylan.h, json={"ips": ["10.0.0.86"]})
    assert res.status_code == 200, res.text
    assert fetched == [1]
    assert _wait(dylan.client, dylan.h, "10.0.0.86")["phase"] == "ready"


def test_saving_the_github_token_fetches_the_build(hub):
    import asyncio

    called = []

    async def check():
        called.append(1)

    hub.tv_app.check = check

    async def go():
        hub.keychecks.after_save("github.tv_app_token")
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert called == [1]
