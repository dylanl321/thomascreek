"""The Android travel phone: joins from anywhere with a code from an admin's iPhone, and plays straight from the
provider when it asks to (Dylan's choice, Nov 2026)."""

from __future__ import annotations

import json

from tests.conftest import Phone
from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import SERVER, _login


def _fixture(name, body):
    """The iPhone decodes this body (JoinCode in ios/Bodies): keep its fixture's shape, like test_ios_bodies."""
    import os

    from tests.test_ios_bodies import FIXTURES, scrub, shape

    path = FIXTURES / f"{name}.json"
    clean = scrub(body)
    clean["code"] = "ABCDEFGHJKLMNPQRSTUV"
    clean["hub"] = "hub-address"  # fixtures carry no addresses (test_fixtures_carry_no_addresses_or_first_names)
    clean["qr"] = "thomascreek://join?hub=hub-address&code=ABCDEFGHJKLMNPQRSTUV"
    if os.environ.get("IOS_FIXTURES") == "update":
        path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n")
    assert shape(json.loads(path.read_text())) == shape(clean), f"{name} changed shape: IOS_FIXTURES=update"


def _code(people, person="avery"):
    dylan = people["phones"]["Dylan"]
    return dylan.secure_post("/v1/devices/join", {"person_id": people[person]["id"], "label": "Travel phone"})


def test_an_admin_code_adds_a_phone_from_away(hub, people, away_client):
    hub.config.hub.remote_url = "https://hub.example"
    made = _code(people)
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["hub"] == "https://hub.example" and body["person"]["name"] == "Avery"
    assert body["qr"].startswith("thomascreek://join?hub=https%3A%2F%2Fhub.example&code=")
    _fixture("devices_join", body)
    code = body["code"]
    assert len(code) == 20 and code not in json.dumps(hub.db.kv_get("device.join_codes"))

    travel = Phone(away_client, "Pixel travel phone")
    plain = away_client.post("/v1/enroll", json={"device_name": "x", "public_key": travel.pub(travel.key)})
    assert plain.status_code == 403 and plain.json()["error"] == "not_home"
    r = away_client.post("/v1/enroll", json={"device_name": travel.name, "model": "Pixel 8", "platform": "Android",
                                             "public_key": travel.pub(travel.key), "join_code": code.lower()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    travel.device_id = r.json()["device_id"]
    signed = travel.sign_in()
    assert signed["role"] == "adult" and signed["person"]["name"] == "Avery"

    other = Phone(away_client, "Someone else")
    again = away_client.post("/v1/enroll", json={"device_name": other.name, "public_key": other.pub(other.key),
                                                 "join_code": code})
    assert again.status_code == 403 and again.json()["error"] == "bad_join_code"


def test_codes_expire_and_only_admins_make_them(hub, people, away_client, monkeypatch):
    hub.config.hub.remote_url = None
    assert _code(people).status_code == 409
    hub.config.hub.remote_url = "https://hub.example"
    avery = people["phones"]["Avery"]
    assert avery.secure_post("/v1/devices/join", {"person_id": people["avery"]["id"]}).status_code == 403
    code = _code(people).json()["code"]
    import hub.joincodes as joincodes
    real = joincodes.time.time
    monkeypatch.setattr(joincodes.time, "time", lambda: real() + joincodes.TTL_S + 1)
    late = Phone(away_client, "Late")
    r = away_client.post("/v1/enroll", json={"device_name": late.name, "public_key": late.pub(late.key),
                                             "join_code": code})
    assert r.status_code == 403 and r.json()["error"] == "bad_join_code"


def test_direct_play_hands_this_phone_the_provider_address(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    r = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                          json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["direct"] is True and body["url"].startswith(f"{SERVER}/movie/household/")
    assert body["url"].endswith("/101.mp4")
    logged = json.dumps([dict(row) for row in hub.db.execute("SELECT * FROM activity").fetchall()])
    assert PASSWORD not in logged and SERVER not in logged
    live = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"channel_key": "xc:thomas-creek:10", "player": "exo", "direct": True}).json()
    assert live["url"].startswith(f"{SERVER}/live/household/") and live["url"].endswith("/10.m3u8")
    holder = f"phone:{dylan.device_id}"
    held = [row for row in hub.db.kv_get("iptv.sessions") or [] if row.get("tv") == holder]
    assert len(held) == 1
    assert dylan.client.post("/v1/tv/phone/hold", headers=dylan.h, json={}).json()["held"] is True
    assert dylan.client.post("/v1/tv/phone/hold", headers=dylan.h, json={"stop": True}).json()["held"] is False
    assert not [row for row in hub.db.kv_get("iptv.sessions") or [] if row.get("tv") == holder]


def test_direct_play_keeps_the_kids_rule(hub, people):
    _library(hub)
    _login(hub)
    theo = people["phones"]["Theo"]
    no = theo.client.post("/v1/tv/phone/play", headers=theo.h,
                          json={"movie_id": "vod:thomas-creek:100", "player": "exo", "direct": True})
    assert no.status_code == 404 and SERVER not in no.text


def test_one_login_one_stream_across_phones(hub, people):
    """Relayed or direct, a phone holds the login while it plays; a second player waits until it is freed."""
    _library(hub)
    _login(hub)
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    first = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:101"})
    assert first.status_code == 200, first.text
    busy = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                             json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert busy.status_code == 503 and busy.json()["error"] == "busy" and SERVER not in busy.text
    # Dylan plays something else: still one lease, not two.
    again = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"})
    assert again.status_code == 200
    assert len([r for r in hub.db.kv_get("iptv.sessions") if r["tv"] == f"phone:{dylan.device_id}"]) == 1
    dylan.client.post("/v1/tv/phone/hold", headers=dylan.h, json={"stop": True})
    ok = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                           json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert ok.status_code == 200, ok.text


def test_an_idle_lease_is_freed(hub, people, monkeypatch):
    import hub.iptvplay as iptvplay

    _library(hub)
    _login(hub)
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    first = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                              json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert first.status_code == 200
    real = iptvplay.time.time
    monkeypatch.setattr(iptvplay.time, "time", lambda: real() + iptvplay.IDLE_S + 1)
    ok = avery.client.post("/v1/tv/phone/play", headers=avery.h,
                           json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True})
    assert ok.status_code == 200, ok.text
