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
    if "download_url" in clean:
        clean["download_url"] = "hub-address/phone?code=ABCDEFGHJKLMNPQRSTUV"
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


# -- the app itself, from the hub with a live code (no GitHub sign-in on the phone) -----------------------------------

def _github(apk, tag="phone-0.1.1"):
    """The TV repository's releases: a TV release, a draft, then the phone pre-release."""
    import httpx

    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer gh-token"
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[
                {"tag_name": "phone-0.2.0", "draft": True, "assets": [{"id": 9, "name": "ThomasCreekPhone.apk"}]},
                {"tag_name": tag, "draft": False, "prerelease": True,
                 "assets": [{"id": 7, "name": "ThomasCreekPhone.apk", "size": len(apk)}]},
                {"tag_name": "tc-1.4.1-tc.31", "draft": False, "assets": [{"id": 5, "name": "ThomasCreekTV.apk"}]},
            ])
        if request.url.path.endswith("/releases/assets/7"):
            return httpx.Response(200, content=apk)
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


def test_the_join_page_hands_out_the_app_from_away(hub, people, away_client):
    from tests.test_tvapp import CERT_A, make_apk

    hub.config.hub.remote_url = "https://hub.example"
    apk = make_apk(CERT_A)
    hub.vault.set("github.tv_app_token", "gh-token")
    transport, seen = _github(apk)
    hub.tv_app._transport = transport
    body = _code(people).json()
    code = body["code"]
    assert body["download_url"] == f"https://hub.example/phone?code={code}"

    page = away_client.get(f"/phone?code={code}")
    assert page.status_code == 200, page.text
    assert "Download Thomas Creek" in page.text and f"/phone/app?code={code}" in page.text
    assert f"thomascreek://join?hub=https%3A%2F%2Fhub.example&amp;code={code}" in page.text
    got = away_client.get(f"/phone/app?code={code.lower()}")
    assert got.status_code == 200 and got.content == apk
    assert got.headers["content-type"] == "application/vnd.android.package-archive"
    assert hub.phone_app.latest()["tag"] == "phone-0.1.1"
    # Checked once, then kept: a second download doesn't ask GitHub again.
    away_client.get(f"/phone/app?code={code}")
    assert seen.count("/repos/dylanl321/thomascreek-tv/releases") == 1

    # The code still joins after the download; once used, the page and the file are gone.
    travel = Phone(away_client, "Travel")
    assert away_client.post("/v1/enroll", json={"device_name": travel.name, "public_key": travel.pub(travel.key),
                                                "join_code": code}).status_code == 200
    assert away_client.get(f"/phone?code={code}").status_code == 403
    assert away_client.get(f"/phone/app?code={code}").status_code == 403


def test_no_code_no_app(hub, away_client, home_client):
    for client in (away_client, home_client):
        assert client.get("/phone/app").status_code == 403
        assert client.get("/phone/app?code=ABCDEFGHJKLMNPQRSTUV").status_code == 403
        assert "expired" in client.get("/phone?code=nope").text


def test_an_unsigned_phone_build_is_not_handed_out(hub, people, away_client):
    from tests.test_tvapp import CERT_A, make_apk

    hub.config.hub.remote_url = "https://hub.example"
    hub.vault.set("github.tv_app_token", "gh-token")
    hub.tv_app._transport, _ = _github(make_apk(CERT_A, signed=False))
    code = _code(people).json()["code"]
    assert "isn't on the hub yet" in away_client.get(f"/phone?code={code}").text
    assert away_client.get(f"/phone/app?code={code}").status_code == 503
    assert hub.phone_app.last_error and "signed" in hub.phone_app.last_error


# -- the phone app updating itself (signed in, with its own bearer) -------------------------------------------------

def _github_with_info(apk, info, tag="phone-0.2.0"):
    import httpx

    def handler(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[
                {"tag_name": tag, "draft": False, "prerelease": True, "assets": [
                    {"id": 7, "name": "ThomasCreekPhone.apk", "size": len(apk)},
                    {"id": 8, "name": "phone-update.json"}]},
            ])
        if request.url.path.endswith("/releases/assets/7"):
            return httpx.Response(200, content=apk)
        if request.url.path.endswith("/releases/assets/8"):
            return httpx.Response(200, json=info)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_a_signed_in_phone_finds_and_downloads_its_update(hub, people):
    import hashlib

    from tests.test_tvapp import CERT_A, make_apk

    apk = make_apk(CERT_A)
    hub.vault.set("github.tv_app_token", "gh-token")
    hub.tv_app._transport = _github_with_info(apk, {"versionCode": 4, "versionName": "0.2.1",
                                                    "sha256": hashlib.sha256(apk).hexdigest(), "notes": "Updates"})
    avery = people["phones"]["Avery"]
    r = avery.client.get("/v1/tv/phone/update?version_code=3", headers=avery.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["up_to_date"] is False and body["version_code"] == 4 and body["version_name"] == "0.2.1"
    assert body["sha256"] == hashlib.sha256(apk).hexdigest() and len(body["signer_sha256"]) == 64
    assert body["apk_path"] == "/v1/tv/phone/update/apk"
    got = avery.client.get(body["apk_path"], headers=avery.h)
    assert got.status_code == 200 and got.content == apk
    same = avery.client.get("/v1/tv/phone/update?version_code=4", headers=avery.h).json()
    assert same == {"up_to_date": True, "version_code": 4}
    # No bearer, no build.
    assert avery.client.get("/v1/tv/phone/update").status_code == 401
    assert avery.client.get("/v1/tv/phone/update/apk").status_code == 401


def test_a_build_that_does_not_match_its_info_is_not_offered(hub, people):
    from tests.test_tvapp import CERT_A, make_apk

    hub.vault.set("github.tv_app_token", "gh-token")
    hub.tv_app._transport = _github_with_info(make_apk(CERT_A), {"versionCode": 4, "sha256": "0" * 64})
    avery = people["phones"]["Avery"]
    r = avery.client.get("/v1/tv/phone/update?version_code=3", headers=avery.h)
    assert r.status_code == 503 and "doesn't match" in r.json()["message"]


def test_a_build_without_a_version_is_still_handed_out_but_not_offered(hub, people):
    from tests.test_tvapp import CERT_A, make_apk

    hub.vault.set("github.tv_app_token", "gh-token")
    hub.tv_app._transport, _ = _github(make_apk(CERT_A))
    avery = people["phones"]["Avery"]
    r = avery.client.get("/v1/tv/phone/update?version_code=3", headers=avery.h)
    assert r.status_code == 503 and r.json()["error"] == "no_version"


def test_the_newest_phone_release_wins_whatever_order_github_lists_them():
    from hub.phoneapp import _release_order
    listed = [{"tag_name": t} for t in ("phone-0.9.0", "phone-0.3.0", "phone-0.10.0", "phone-0.1.0-test")]
    assert max(listed, key=_release_order)["tag_name"] == "phone-0.10.0"


def test_direct_play_also_offers_the_hub_relay_on_the_same_login(hub, people, monkeypatch):
    """Abroad the provider may turn the phone's own address away: the hub's relay is the last thing to try."""
    _library(hub)
    _login(hub)
    monkeypatch.setattr(hub.config.hub, "remote_url", "https://hub.example/")
    dylan = people["phones"]["Dylan"]
    live = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"channel_key": "xc:thomas-creek:10", "player": "exo", "direct": True}).json()
    assert live["relay_url"].startswith("/v1/tv/phone/live/") and live["relay_url"].endswith("/index.m3u8")
    assert live["alternates"][-1] == "https://hub.example" + live["relay_url"]
    assert SERVER not in live["relay_url"]
    holder = f"phone:{dylan.device_id}"
    assert len([row for row in hub.db.kv_get("iptv.sessions") or [] if row.get("tv") == holder]) == 1
    claims = hub.tokens.verify(live["relay_url"].split("/")[5], "tv-phone")
    held = next(row for row in hub.db.kv_get("iptv.sessions") if row.get("tv") == holder)
    assert claims["pl"] == f"{held['provider']}|{held['login']}"
