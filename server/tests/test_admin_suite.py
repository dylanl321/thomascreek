"""The admin suite: an Overview that lists what needs someone, area pages, and TVs that say why they're in a state.

The home-screen check is the case that confused us: a kids' TV asked for several apps with "only these apps" on,
only one of them was installed, so the TV showed just that one and blocked everything else.
"""

from __future__ import annotations

import re
import time
from urllib.parse import unquote

from hub import tvlauncher, tvstate

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token
from tests.test_tv_downloads import _sign_in
from tests.test_tv_fleet import BODY

KIDS = "com.google.android.youtube.tvkids"
DISNEY = "com.disney.disneyplus"
NETFLIX = "com.netflix.ninja"


def _tv(hub, home_client, name="Kids' room"):
    key = Key()
    _enroll(home_client, key, TV_ID, name)
    hub.tv_app.set_tv(TV_ID, status="approved", room="kids_room")
    token = _token(home_client, key, Clock())["access_token"]
    return _auth(token)


def _netflix_only(hub, people, home_client):
    headers = _tv(hub, home_client)
    home_client.post("/v1/tv/apps/installed", headers=headers, json={"apps": [
        {"package": NETFLIX, "label": "Netflix"}, {"package": "com.google.android.youtube.tv", "label": "YouTube"}]})
    tvlauncher.set_launcher(hub, TV_ID, {"on": True, "person_id": people["theo"]["id"],
                                         "apps": [KIDS, DISNEY, NETFLIX], "guard": True}, apply=False)
    return headers


def test_a_kids_tv_missing_its_apps_says_why_and_can_be_fixed(hub, people, home_client):
    headers = _netflix_only(hub, people, home_client)
    home_client.post("/v1/tv/fleet/report", headers=headers, json=BODY)

    hs = tvstate.home_screen(hub.tv_app.tvs()[TV_ID])
    assert hs["mode"] == "only" and hs["shows"] == [NETFLIX] and hs["missing"] == [KIDS, DISNEY]
    assert hs["state"] == "bad" and "YouTube Kids, Disney+ aren't installed" in hs["why"]
    row = tvstate.one(hub, TV_ID)
    assert row["state"] == "bad" and row["label"] == "Home screen differs"

    csrf = _sign_in(home_client, people)
    overview = home_client.get("/admin").text
    assert "Needs you" in overview and "Kids&#x27; room: home screen differs" in overview
    assert f"/admin/devices/{TV_ID}?tab=home" in overview
    assert re.search(r"href=/admin/devices[^>]*>.*?<span class='n bad'>1</span>", overview)

    tvs = home_client.get("/admin/devices").text
    assert "Home screen differs" in tvs and "Shows 1 of 3 picked apps" in tvs

    page = home_client.get(f"/admin/devices/{TV_ID}?tab=home").text
    assert "You asked for" in page and "The TV reports" in page and "Not installed on this TV" in page
    assert "Open in the Play Store on the TV" in page and "Theo (child: kids rows only)" in page

    # No network debugging: the store button says what to do instead of failing silently.
    store = home_client.post(f"/admin/devices/{TV_ID}/launcher/store", data={"csrf": csrf, "package": KIDS},
                             follow_redirects=False)
    where = unquote(store.headers["location"])
    assert store.status_code == 303 and "tab=home" in where and "Install the app from the Play Store" in where

    dropped = home_client.post(f"/admin/devices/{TV_ID}/launcher/drop-missing", data={"csrf": csrf},
                               follow_redirects=False)
    assert dropped.status_code == 303
    block = tvlauncher.view(hub.tv_app.tvs()[TV_ID])
    assert block["apps"] == [NETFLIX] and block["guard"] is True
    assert tvstate.home_screen(hub.tv_app.tvs()[TV_ID])["state"] == "ok"


def test_the_home_screen_modes_map_to_the_launcher(hub, people, home_client):
    headers = _tv(hub, home_client)
    netflix = [{"package": NETFLIX, "label": "Netflix"}]
    home_client.post("/v1/tv/apps/installed", headers=headers, json={"apps": netflix})
    csrf = _sign_in(home_client, people)
    url = f"/admin/devices/{TV_ID}/launcher"
    only = home_client.post(url, data={"csrf": csrf, "mode": "only", "app": [NETFLIX]}, follow_redirects=False)
    assert only.status_code == 303 and "tab=home" in only.headers["location"]
    assert tvlauncher.view(hub.tv_app.tvs()[TV_ID]) == {"on": True, "person_id": None, "apps": [NETFLIX], "guard": True}
    home_client.post(url, data={"csrf": csrf, "mode": "all", "app": [NETFLIX]})
    assert tvlauncher.view(hub.tv_app.tvs()[TV_ID])["guard"] is False
    off = home_client.post(url, data={"csrf": csrf, "mode": "tv_own"}, follow_redirects=False)
    assert "own home screen" in unquote(off.headers["location"])
    assert tvlauncher.view(hub.tv_app.tvs()[TV_ID])["on"] is False
    refused = home_client.post(url, data={"csrf": csrf, "mode": "only"}, follow_redirects=False)
    assert "Pick" in refused.headers["location"]


def test_the_three_signals_and_off(hub, people, home_client):
    headers = _tv(hub, home_client, "Basement")
    now = time.time()
    row = tvstate.one(hub, TV_ID, now)
    assert row["signals"]["app"]["state"] == "ok"
    assert row["signals"]["report"]["state"] == "warn" and "Never reported" in row["signals"]["report"]["text"]
    assert row["signals"]["link"]["state"] == "warn" and row["state"] == "warn" and row["label"] == "Limited control"

    home_client.post("/v1/tv/fleet/report", headers=headers, json=BODY)
    assert tvstate.one(hub, TV_ID)["signals"]["report"]["state"] == "ok"

    boxes = hub.tv_app.boxes()
    boxes["10.0.0.86"] = {"name": "Basement", "state": "ok", "checked_at": now}
    hub.tv_app._save_boxes(boxes)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["ip"] = "10.0.0.86"
    hub.tv_app._save_tvs(tvs)
    assert tvstate.one(hub, TV_ID)["signals"]["link"]["state"] == "ok"
    assert tvstate.one(hub, TV_ID)["state"] == "ok"

    later = tvstate.one(hub, TV_ID, now + 6 * 3600)
    assert later["state"] == "off" and later["label"] == "Off or asleep"
    assert tvstate.attention(hub, now + 6 * 3600) == []  # an asleep TV isn't a problem


def test_an_apple_tv_has_no_android_signals_or_home_tab(hub, people, home_client):
    _tv(hub, home_client, "Office")
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["model"] = "AppleTV14,1"
    hub.tv_app._save_tvs(tvs)
    row = tvstate.one(hub, TV_ID)
    assert row["apple"] and row["home"] is None and row["state"] == "ok"
    assert row["signals"]["report"]["state"] == "na" and row["signals"]["link"]["state"] == "na"
    _sign_in(home_client, people)
    page = home_client.get(f"/admin/devices/{TV_ID}?tab=home").text
    assert "Home screen</a>" not in page and "Why it's in this state" in page


def test_every_area_opens_and_forms_return_to_it(hub, people, home_client):
    csrf = _sign_in(home_client, people)
    expect = {
        "/admin/connections": "Keys and files",
        "/admin/watching": "IPTV providers",
        "/admin/ai": "AI connections (MCP)",
        "/admin/updates": "Test simulators",
        "/admin/security": "TLS pin",
    }
    for path, text in expect.items():
        page = home_client.get(path)
        assert page.status_code == 200 and text in page.text, path
        assert f"<a href={path} class=on>" in page.text
    assert "class=savebar" in home_client.get("/admin/connections").text
    back = home_client.post("/admin/services/check", data={"csrf": csrf}, follow_redirects=False,
                            headers={"referer": "https://hub.local:8443/admin/connections"})
    assert back.headers["location"].startswith("/admin/connections?m=")
    plain = home_client.post("/admin/services/check", data={"csrf": csrf}, follow_redirects=False,
                             headers={"referer": "https://elsewhere.example/somewhere"})
    assert plain.headers["location"].startswith("/admin?m=")
    # Downloads sits under Watching in the menu.
    assert "<a href=/admin/watching class=on>" in home_client.get("/admin/downloads").text


def test_area_pages_need_a_session_and_the_home_network(hub, home_client, away_client):
    for path in ("/admin/connections", "/admin/watching", "/admin/ai", "/admin/updates", "/admin/security"):
        assert away_client.get(path).status_code == 403
        assert "Sign in with the app" in home_client.get(path).text
