"""Backup addresses: one provider on several domains with the same logins, so a blocked address isn't an outage."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx

from hub import iptvaddr, iptvplay
from tests.test_admin_page import _sign_in
from tests.test_iptv_play import _ready
from tests.test_tv_api import PASSWORD, TV_ID, _library
from tests.test_tv_phone import SERVER, _login

BACKUP = "http://backup.example:8080"


def test_addresses_are_tried_best_first_and_come_back(hub):
    db = hub.db
    good, bad = iptvaddr.parse_list("http://b.test:80/\nhttps://c.test, not an address  http://b.test:80")
    assert good == ["http://b.test:80", "https://c.test"] and bad == ["not"] + ["an", "address"]
    assert iptvaddr.set_backups(db, "alpha", good + ["http://a.test"], primary="http://a.test") == good
    assert iptvaddr.ordered(db, "alpha", "http://a.test", now=1000) == ["http://a.test", "http://b.test:80", "https://c.test"]
    iptvaddr.note(db, "alpha", "http://a.test", False, "couldn't be reached", now=1000)
    assert iptvaddr.best(db, "alpha", "http://a.test", now=1001) == "http://b.test:80"
    rows = iptvaddr.status(db, "alpha", "http://a.test", now=1001)
    assert rows[0]["main"] and rows[0]["state"] == "down" and not rows[0]["in_use"] and rows[1]["in_use"]
    # Tried again after DOWN_S, and back first once it answers.
    assert iptvaddr.best(db, "alpha", "http://a.test", now=1000 + iptvaddr.DOWN_S + 1) == "http://a.test"
    iptvaddr.note(db, "alpha", "http://a.test", True, now=1002)
    assert iptvaddr.best(db, "alpha", "http://a.test", now=1003) == "http://a.test"
    # No backups: nothing changes.
    assert iptvaddr.ordered(db, "beta", "http://only.test") == ["http://only.test"]
    iptvaddr.forget(db, "alpha")
    assert iptvaddr.backups(db, "alpha") == []


def test_check_tells_a_block_page_from_the_provider():
    def run(response=None, raise_=None):
        with respx.mock() as mock:
            route = mock.get("http://x.test/player_api.php")
            route.mock(side_effect=raise_) if raise_ else route.mock(return_value=response)
            return asyncio.run(iptvaddr.check("http://x.test", "u", "p"))

    assert run(httpx.Response(200, json={"user_info": {"auth": 1}})) == (True, "answered")
    assert run(httpx.Response(401))[0] is True
    assert run(httpx.Response(200, text="<html>This site is blocked</html>"))[0] is False
    assert run(httpx.Response(302, headers={"location": "http://block.isp/"}))[0] is False
    assert run(httpx.Response(503))[0] is False
    assert run(raise_=httpx.ConnectError("no"))[0] is False


def test_logins_use_the_working_address_and_the_tv_config_keeps_the_main_one(hub):
    _ready_logins(hub)
    iptvaddr.set_backups(hub.db, "alpha", [BACKUP], "http://alpha.test")
    iptvaddr.note(hub.db, "alpha", "http://alpha.test", False, "couldn't be reached")
    row = next(lg for lg in iptvplay.logins(hub) if lg["provider"] == "alpha")
    assert row["server"] == BACKUP and row["primary"] == "http://alpha.test" and row["servers"] == [BACKUP, "http://alpha.test"]
    p = next(p for p in iptvplay.providers(hub) if p["id"] == "alpha")
    assert p["server"] == "http://alpha.test" and p["backups"] == [BACKUP]
    # A TV keys its own list by the main address, so that stays; the backups ride along (optional field).
    pl = next(pl for pl in hub.tv_app._source()["playlists"] if pl["username"] == "house")
    assert pl["server"] == "http://alpha.test" and pl["servers"] == [BACKUP, "http://alpha.test"]
    beta = next(pl for pl in hub.tv_app._source()["playlists"] if pl["username"] == "spare")
    assert "servers" not in beta


def _ready_logins(hub):
    hub.config.iptv.providers = []
    hub.vault.set("iptv.alpha.house.password", "secret-house")
    hub.vault.set("iptv.beta.spare.password", "secret-spare")
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "house", "server": "http://alpha.test", "username": "house",
         "password_secret": "iptv.alpha.house.password"},
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare",
         "password_secret": "iptv.beta.spare.password"},
    ])


def test_a_tv_failure_on_a_blocked_address_moves_to_the_backup_on_the_same_login(hub, people, home_client, monkeypatch):
    canonical, tv_headers = _ready(hub, home_client)
    iptvaddr.set_backups(hub.db, "alpha", [BACKUP], "http://alpha.test")
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tv/tune", headers=dylan.h, json={"canonical_id": canonical, "device": TV_ID}).json()
    opened = home_client.get(body["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.headers["location"].startswith("http://alpha.test/movie/house/")

    async def blocked(url, username, password, transport=None):
        return (url != "http://alpha.test", "couldn't be reached")
    monkeypatch.setattr(iptvaddr, "check", blocked)
    nxt = home_client.post("/v1/tv/tune/fail", headers=tv_headers, json={"session": body["session"]}).json()
    assert nxt["ok"] is True and nxt["stream"]["ref"] == "vod:alpha:100" and nxt["session"] != body["session"]
    again = home_client.get(nxt["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert again.headers["location"].startswith(f"{BACKUP}/movie/house/")
    assert iptvaddr.status(hub.db, "alpha", "http://alpha.test")[0]["state"] == "down"
    logged = json.dumps([dict(r) for r in hub.db.execute("SELECT * FROM activity").fetchall()])
    assert "alpha.test" not in logged and "backup.example" not in logged

    # The address answers, so the stream itself failed: the next login, as before.
    async def fine(url, username, password, transport=None):
        return (True, "answered")
    monkeypatch.setattr(iptvaddr, "check", fine)
    last = home_client.post("/v1/tv/tune/fail", headers=tv_headers, json={"session": nxt["session"]}).json()
    assert last["ok"] is True and last["stream"]["ref"] == "vod:beta:55"


def test_direct_phone_play_lists_the_backup_addresses(hub, people):
    _library(hub)
    _login(hub)
    iptvaddr.set_backups(hub.db, "thomas-creek", [BACKUP], SERVER)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": "vod:thomas-creek:101", "player": "exo", "direct": True}).json()
    assert body["url"].startswith(f"{SERVER}/movie/household/")
    assert [u.split("/movie/")[0] for u in body["alternates"]] == [BACKUP]
    assert body["alternates"][0].endswith("/101.mp4")
    logged = json.dumps([dict(r) for r in hub.db.execute("SELECT * FROM activity").fetchall()])
    assert PASSWORD not in logged and BACKUP not in logged
    # Relayed play (the iPhone) moves to the backup by itself when the main address doesn't connect.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/movie/household/{PASSWORD}/101.mp4").mock(side_effect=httpx.ConnectError("blocked"))
        mock.get(f"{BACKUP}/movie/household/{PASSWORD}/101.mp4").mock(return_value=httpx.Response(200, content=b"film"))
        relayed = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                                    json={"movie_id": "vod:thomas-creek:101"}).json()
        got = dylan.client.get(relayed["url"])
    assert got.status_code == 200 and got.content == b"film"
    assert iptvaddr.best(hub.db, "thomas-creek", SERVER) == BACKUP


def test_the_catalog_reads_from_the_backup_when_the_main_address_is_blocked(hub):
    _library(hub)
    _login(hub)
    iptvaddr.set_backups(hub.db, "thomas-creek", [BACKUP], SERVER)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/player_api.php").mock(side_effect=httpx.ConnectError("blocked"))
        mock.get(f"{BACKUP}/player_api.php").mock(return_value=httpx.Response(200, json={
            "info": {"name": "The Film", "plot": "Read from the backup."}}))
        page = asyncio.run(hub.tv_catalog.fetch_movie("vod:thomas-creek:999"))
    assert page["plot"] == "Read from the backup."
    assert hub.tv_catalog._login()[0] == BACKUP


def test_admin_page_saves_and_shows_backup_addresses(hub, people, home_client):
    csrf = _sign_in(people, home_client)
    r = home_client.post("/admin/iptv/provider/add", data={
        "csrf": csrf, "name": "Strong", "server": "http://strong.test:8080", "username": "u1", "password": "pw-1",
        "backups": "http://strong2.test:8080\nhttp://strong3.test"}, follow_redirects=False)
    assert "Added%20Strong" in r.headers["location"]
    assert iptvaddr.backups(hub.db, "thomas-creek") == ["http://strong2.test:8080", "http://strong3.test"]
    page = home_client.get("/admin/watching").text
    assert "http://strong3.test" in page and "Backup addresses" in page and "Main address" in page
    r = home_client.post("/admin/iptv/provider/save", data={
        "csrf": csrf, "provider": "thomas-creek", "name": "Strong", "server": "http://strong.test:8080",
        "backups": "nope"}, follow_redirects=False)
    assert "isn%27t%20an%20address" in r.headers["location"] or "isn't" in r.headers["location"]
    home_client.post("/admin/iptv/provider/save", data={
        "csrf": csrf, "provider": "thomas-creek", "name": "Strong", "server": "http://strong.test:8080", "backups": ""})
    assert iptvaddr.backups(hub.db, "thomas-creek") == []
