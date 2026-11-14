"""The hub web page's status parts (key checks, services, attention, warnings) and the People page."""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub import keycheck
from hub.recentlog import RECENT, install

from .conftest import Phone


def _sign_in(people, client) -> str:
    url = people["phones"]["Dylan"].secure_post("/v1/admin/web-link", {}).json()["url"]
    assert client.get(url, follow_redirects=False).status_code == 303
    page = client.get("/admin").text
    return re.search(r"name=csrf value=([0-9a-f]+)", page).group(1)


def _plex_on(hub):
    hub.config.plex.enabled, hub.config.plex.url = True, "http://plex.test"


def test_key_test_button_shows_works_or_refused_without_the_value(hub, people, home_client):
    _plex_on(hub)
    csrf = _sign_in(people, home_client)
    home_client.post("/admin/secrets", data={"csrf": csrf, "name": "plex.token", "value": "plex-secret-123"})
    with respx.mock() as mock:
        mock.get("http://plex.test/library/sections").mock(return_value=httpx.Response(401))
        r = home_client.post("/admin/keys/test", data={"csrf": csrf, "name": "plex.token"})
    assert "Plex refused it" in r.text
    page = home_client.get("/admin").text
    assert "Not working" in page and "plex-secret-123" not in page
    assert "Needs attention" in page
    assert hub.keychecks.results["plex"]["state"] == "bad"
    with respx.mock() as mock:
        mock.get("http://plex.test/library/sections").mock(return_value=httpx.Response(200, json={}))
        home_client.post("/admin/keys/test", data={"csrf": csrf, "name": "plex.token"})
    assert hub.keychecks.results["plex"]["state"] == "ok"
    assert ">Works<" in home_client.get("/admin").text
    # The status JSON lists states, never values.
    detail = home_client.get("/admin/status.json?detail=1").json()
    assert {"id": "plex", "state": "ok"}.items() <= next(k for k in detail["keys"] if k["id"] == "plex").items()
    assert "plex-secret-123" not in str(detail)
    # Without the form token nothing runs.
    assert home_client.post("/admin/keys/test", data={"name": "plex.token"}).status_code == 403


def test_unreachable_service_is_not_called_a_bad_key(hub):
    _plex_on(hub)
    hub.vault.set("plex.token", "x" * 20)
    with respx.mock() as mock:
        mock.get("http://plex.test/library/sections").mock(side_effect=httpx.ConnectError("nope"))
        asyncio.run(hub.keychecks.run(secret="plex.token"))
    assert hub.keychecks.results["plex"]["state"] == "down"


def test_key_that_stops_working_tells_admins_once(hub, people):
    hub.vault.set("anthropic.api_key", "sk-ant-test-0000000000")
    with respx.mock() as mock:
        route = mock.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
        asyncio.run(hub.keychecks.run(ids=["anthropic"]))
        assert route.calls[0].request.headers["x-api-key"] == "sk-ant-test-0000000000"
        route.mock(return_value=httpx.Response(401, json={"error": {"type": "authentication_error"}}))
        asyncio.run(hub.keychecks.run(ids=["anthropic"]))
        asyncio.run(hub.keychecks.run(ids=["anthropic"]))
    rows = hub.db.all("SELECT * FROM inbox WHERE group_key='keycheck:anthropic'")
    assert len(rows) == 1 and "stopped working" in rows[0]["title"] and "sk-ant" not in str(rows)


def test_github_token_expiry_and_xtream_account(hub):
    soon = time.gmtime(time.time() + 5 * 86400)
    hub.vault.set("github.tv_app_token", "github_pat_x")
    hub.vault.set("iptv.xtream_server", "http://iptv.test")
    hub.vault.set("iptv.xtream_username", "me")
    hub.vault.set("iptv.xtream_password", "pw-123456789")
    exp = str(int(time.time() + 200 * 86400))
    with respx.mock() as mock:
        mock.get("https://api.github.com/repos/dylanl321/thomascreek-tv").mock(return_value=httpx.Response(
            200, json={}, headers={"github-authentication-token-expiration": time.strftime("%Y-%m-%d %H:%M:%S UTC", soon)}))
        mock.get("http://iptv.test/player_api.php").mock(return_value=httpx.Response(200, json={
            "user_info": {"auth": 1, "status": "Active", "exp_date": exp, "max_connections": "2", "active_cons": "1"}}))
        asyncio.run(hub.keychecks.run())
    gh = hub.keychecks.results["github"]
    assert gh["state"] == "warn" and gh["detail"].startswith("expires")
    tv = hub.keychecks.results["iptv:thomas-creek:main"]
    assert tv["state"] == "ok" and "1 of 2 streams" in tv["detail"]
    with respx.mock() as mock:
        mock.get("http://iptv.test/player_api.php").mock(return_value=httpx.Response(200, json={"user_info": {"auth": 0}}))
        asyncio.run(hub.keychecks.run(secret="iptv.xtream_password"))
    assert hub.keychecks.results["iptv:thomas-creek:main"]["state"] == "bad"


def test_apns_key_checked_against_apple(hub):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    hub.config.apns.team_id = "TEAM123456"
    hub.vault.set("apns.auth_key_p8", pem)
    hub.vault.set("apns.key_id", "ABCDE12345")
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\.push\.apple\.com/3/device/0+").mock(
            return_value=httpx.Response(400, json={"reason": "BadDeviceToken"}))
        asyncio.run(hub.keychecks.run(ids=["apns"]))
    assert hub.keychecks.results["apns"]["state"] == "ok"
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\.push\.apple\.com/3/device/0+").mock(
            return_value=httpx.Response(403, json={"reason": "InvalidProviderToken"}))
        asyncio.run(hub.keychecks.run(ids=["apns"]))
    assert hub.keychecks.results["apns"]["state"] == "bad" and "Key ID" in hub.keychecks.results["apns"]["detail"]


def test_saving_a_key_drops_the_old_cached_client(hub):
    hub.config.firewalla.enabled, hub.config.firewalla.msp_domain = True, "fw.test"
    hub.vault.set("firewalla.msp_token", "old")
    client = hub.firewalla._c()
    assert client.headers["Authorization"] == "Token old"
    hub.vault.set("firewalla.msp_token", "new")
    keycheck.forget_cached(hub, "firewalla.msp_token")
    assert hub.firewalla._c().headers["Authorization"] == "Token new"


def test_services_and_warnings_on_the_page(hub, people, home_client):
    _plex_on(hub)
    hub.vault.set("plex.token", "tok-abcdefgh-1234")
    csrf = _sign_in(people, home_client)
    with respx.mock() as mock:
        mock.get("http://plex.test/library/sections").mock(side_effect=httpx.ConnectError("x"))
        r = home_client.post("/admin/services/check", data={"csrf": csrf})
    assert "Down: Plex" in r.text
    assert hub.plex.down_since and hub.plex.last_checked and hub.plex.last_ms is not None
    install()
    logging.getLogger("hub.test").warning("leaked tok-abcdefgh-1234 in a message")
    page = home_client.get("/admin").text
    assert "Recent warnings" in page and "tok-abcdefgh-1234" not in page and "leaked ••• in a message" in page
    logging.getLogger().removeHandler(RECENT)


def test_people_page_approve_move_remove(hub, people, home_client):
    csrf = _sign_in(people, home_client)
    new = Phone(home_client, "Juni's iPad")
    new.enroll()
    page = home_client.get("/admin/people").text
    assert "Waiting for you" in page and "Juni&#x27;s iPad" in page
    assert "1 device waiting" in home_client.get("/admin").text
    theo = people["theo"]
    r = home_client.post("/admin/people/assign", data={"csrf": csrf, "device": new.device_id, "person": theo["id"]})
    assert "Approved" in r.text
    d = hub.db.device(new.device_id)
    assert d["status"] == "active" and d["person_id"] == theo["id"]
    # Move it to Avery.
    home_client.post("/admin/people/assign", data={"csrf": csrf, "device": new.device_id, "person": people["avery"]["id"]})
    assert hub.db.device(new.device_id)["person_id"] == people["avery"]["id"]
    # Remove it.
    r = home_client.post("/admin/people/remove-device", data={"csrf": csrf, "device": new.device_id})
    assert "Removed" in r.text and hub.db.device(new.device_id)["status"] == "revoked"
    assert "Removed devices (1)" in r.text
    # The device this page signed in with stays.
    mine = people["phones"]["Dylan"].device_id
    r = home_client.post("/admin/people/remove-device", data={"csrf": csrf, "device": mine})
    assert "stays" in r.text and hub.db.device(mine)["status"] == "active"
    # No form token, no change.
    assert home_client.post("/admin/people/remove-device", data={"device": mine}).status_code == 403


def test_people_page_roles_and_adding(hub, people, home_client):
    csrf = _sign_in(people, home_client)
    r = home_client.post("/admin/people/add", data={"csrf": csrf, "name": "  Lauren  ", "role": "adult"})
    assert "Added Lauren" in r.text
    assert any(p["name"] == "Lauren" and p["role"] == "adult" for p in hub.db.people())
    assert "already someone" in home_client.post("/admin/people/add", data={"csrf": csrf, "name": "lauren", "role": "adult"}).text
    assert "needed" in home_client.post("/admin/people/add", data={"csrf": csrf, "name": "X", "role": "pending"}).text
    r = home_client.post("/admin/people/role", data={"csrf": csrf, "person": people["avery"]["id"], "role": "guest"})
    assert hub.db.person(people["avery"]["id"])["role"] == "guest"
    r = home_client.post("/admin/people/role", data={"csrf": csrf, "person": people["dylan"]["id"], "role": "adult"})
    assert "own role" in r.text and hub.db.person(people["dylan"]["id"])["role"] == "admin"


def test_people_page_needs_sign_in_and_home(people, home_client, away_client):
    assert away_client.get("/admin/people").status_code == 403
    assert "Sign in with the app" in home_client.get("/admin/people").text
