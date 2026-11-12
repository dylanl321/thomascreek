from __future__ import annotations

import asyncio

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub.appupdate import ASC, AppUpdates

BUILDS = {
    "data": [
        {"type": "builds", "id": "b18", "attributes": {"version": "18", "uploadedDate": "2026-11-12T17:00:00Z",
                                                       "processingState": "VALID", "expired": False},
         "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "v2"}},
                           "buildBetaDetail": {"data": {"type": "buildBetaDetails", "id": "d18"}}}},
        {"type": "builds", "id": "b17", "attributes": {"version": "17", "uploadedDate": "2026-11-12T16:00:00Z",
                                                       "processingState": "VALID", "expired": False},
         "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "v2"}},
                           "buildBetaDetail": {"data": {"type": "buildBetaDetails", "id": "d17"}}}},
        {"type": "builds", "id": "b9", "attributes": {"version": "9", "processingState": "VALID", "expired": True}},
    ],
    "included": [
        {"type": "preReleaseVersions", "id": "v2", "attributes": {"version": "2.0"}},
        {"type": "buildBetaDetails", "id": "d18", "attributes": {"internalBuildState": "MISSING_EXPORT_COMPLIANCE"}},
        {"type": "buildBetaDetails", "id": "d17", "attributes": {"internalBuildState": "IN_BETA_TESTING"}},
    ],
}


def test_pick_skips_builds_testers_cant_install():
    assert AppUpdates.pick(BUILDS) == {"build": 17, "version": "2.0", "uploaded": "2026-11-12T16:00:00Z"}
    assert AppUpdates.pick({"data": []}) is None


def _p8() -> str:
    k = ec.generate_private_key(ec.SECP256R1())
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


def test_check_asks_app_store_connect(hub, tmp_path):
    au = hub.app_update
    au.path = tmp_path / "app_update.json"
    assert asyncio.run(au.check()) is None  # no key yet: nothing to ask
    hub.vault.set("appstore.auth_key_p8", _p8())
    hub.vault.set("appstore.key_id", "ABCDE12345")
    hub.vault.set("appstore.issuer_id", "11111111-2222-3333-4444-555555555555")
    with respx.mock() as mock:
        apps = mock.get(f"{ASC}/v1/apps").mock(return_value=httpx.Response(200, json={"data": [{"type": "apps", "id": "6700000001"}]}))
        builds = mock.get(f"{ASC}/v1/builds").mock(return_value=httpx.Response(200, json=BUILDS))
        found = asyncio.run(au.check())
        assert found and found["build"] == 17 and au.last_error is None
        assert apps.calls[0].request.url.params["filter[bundleId]"] == "me.dlewis.thomascreek"
        assert builds.calls[0].request.url.params["filter[app]"] == "6700000001"
        assert builds.calls[0].request.headers["authorization"].startswith("Bearer ey")
    info = au.info()
    assert info["latest_build"] == 17 and info["latest_version"] == "2.0" and info["source"] == "testflight"
    assert info["testflight_url"] == "itms-beta://beta.itunes.apple.com/v1/app/6700000001"


def test_check_reports_a_bad_key(hub, tmp_path):
    au = hub.app_update
    hub.config.app_update.apple_id = None
    hub.vault.set("appstore.auth_key_p8", _p8())
    hub.vault.set("appstore.key_id", "ABCDE12345")
    hub.vault.set("appstore.issuer_id", "x")
    with respx.mock() as mock:
        mock.get(f"{ASC}/v1/apps").mock(return_value=httpx.Response(401, json={"errors": [{"title": "NOT_AUTHORIZED"}]}))
        assert asyncio.run(au.check()) is None
    assert "turned the key down" in au.last_error


def test_update_route(hub, people, home_client, tmp_path):
    hub.app_update.path = tmp_path / "app_update.json"
    r = home_client.get("/v1/app/update", params={"build": 16})
    assert r.status_code == 200
    assert r.json()["latest_build"] is None and r.json()["update_available"] is False
    hub.app_update.found = {"build": 17, "version": "2.0", "uploaded": "2026-11-12T16:00:00Z"}
    hub.app_update.apple_id = "6700000001"
    j = home_client.get("/v1/app/update", params={"build": 16}).json()
    assert j["update_available"] is True and j["update_required"] is False and j["latest_build"] == 17
    # A required floor: never above what TestFlight has.
    hub.app_update.save_settings(min_build=99)
    j = home_client.get("/v1/app/update", params={"build": 16}).json()
    assert j["min_build"] == 17 and j["update_required"] is True
    assert home_client.get("/v1/app/update", params={"build": 17}).json()["update_required"] is False
    # A signed-in phone's build is remembered for the web page.
    dylan = people["phones"]["Dylan"]
    home_client.get("/v1/app/update", params={"build": 16, "version": "2.0"}, headers=dylan.h)
    assert hub.db.device(dylan.device_id)["app_version"] == "2.0 (16)"
    # A build typed by hand wins when it is newer.
    hub.app_update.save_settings(latest_build=20, min_build=None)
    j = home_client.get("/v1/app/update", params={"build": 17}).json()
    assert j["latest_build"] == 20 and j["source"] == "manual" and j["min_build"] is None


def test_admin_page_app_updates(hub, people, home_client, tmp_path):
    hub.app_update.path = tmp_path / "app_update.json"
    dylan = people["phones"]["Dylan"]
    home_client.get("/v1/app/update", params={"build": 15, "version": "2.0"}, headers=dylan.h)
    url = dylan.secure_post("/v1/admin/web-link", {}).json()["url"]
    home_client.get(url, follow_redirects=False)
    page = home_client.get("/admin").text
    assert "App updates · latest not known yet" in page and "App Store Connect key" in page
    import re as _re
    csrf = _re.search(r"name=csrf value=(\w+)", page).group(1)
    r = home_client.post("/admin/app-update", data={"csrf": csrf, "min_build": "16", "latest_build": "17"})
    assert "Saved" in r.text
    assert "latest build 17 set by hand" in r.text
    assert "2.0 (15) · <b>behind</b>" in r.text and "1 behind" in r.text
    assert home_client.get("/v1/app/update", params={"build": 15}).json()["update_required"] is True
    # A second AuthKey (push already has one) is taken as the App Store Connect key.
    hub.vault.set("apns.key_id", "PUSH000001")
    r = home_client.post("/admin/upload", data={"csrf": csrf},
                         files=[("files", ("AuthKey_ASC0000001.p8", _p8().encode()))])
    assert "App Store Connect key" in r.text and str(hub.vault.get("appstore.key_id")) == "ASC0000001"
    assert str(hub.vault.get("apns.key_id")) == "PUSH000001"
