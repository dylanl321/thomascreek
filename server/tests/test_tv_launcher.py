"""Launcher: Thomas Creek TV is a TV's home screen, optionally signed in as one person, with the hub's apps."""

from __future__ import annotations

import time

from hub import tvlauncher

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _config, _enroll, _token
from tests.test_tv_downloads import _sign_in
from tests.test_tv_fleet import BODY
from tests.test_tvsetup import ScriptAdb, _replies

ALIAS = "me.dlewis.thomascreek.tv/com.ipvio.stream.Home"
GUARD = "me.dlewis.thomascreek.tv/com.thomascreek.tv.device.GuardService"


def _tv(hub, home_client):
    key = Key()
    _enroll(home_client, key, TV_ID, "Theo's TV")
    hub.tv_app.set_tv(TV_ID, status="approved", room="theo_room")
    token = _token(home_client, key, Clock())["access_token"]
    return key, _auth(token)


def test_a_launcher_reaches_the_tv_and_narrows_its_apps(hub, people, home_client):
    key, headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    theo = people["theo"]
    before = home_client.get("/v1/tv/apps", headers=headers).json()["allowed"]
    assert "com.google.android.youtube.tvkids" not in before or before

    put = dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h, json={
        "on": True, "person_id": theo["id"],
        "apps": ["com.google.android.youtube.tvkids", "com.disney.disneyplus", "me.dlewis.thomascreek.tv",
                 "com.google.android.youtube.tvkids"], "guard": True,
    })
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["apps"] == ["com.google.android.youtube.tvkids", "com.disney.disneyplus"]
    assert body["guard"] is True and body["on"] is True
    assert hub.tv_app.tvs()[TV_ID]["profile"] == "kids"

    assert home_client.get("/v1/tv/apps", headers=headers).json() == {
        "allowed": ["com.google.android.youtube.tvkids", "com.disney.disneyplus"]}
    ts = int(time.time())
    config = _config(home_client, key, ts).json()
    assert config["launcher"] == {"on": True, "person_id": theo["id"], "guard": True,
                                  "apps": ["com.google.android.youtube.tvkids", "com.disney.disneyplus"]}

    assert dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h,
                            json={"on": True, "person_id": "nobody"}).status_code == 400
    assert dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h,
                            json={"on": True, "guard": True}).status_code == 400
    # Any TV, nobody pinned, no list: the home screen with every app, no guard.
    open_tv = dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h, json={"on": True}).json()
    assert open_tv["on"] is True and open_tv["guard"] is False and open_tv["apps"] == []
    # The profile's own list again (kids, since Theo was pinned a moment ago), not the launcher's two.
    assert len(home_client.get("/v1/tv/apps", headers=headers).json()["allowed"]) > 2
    assert dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h,
                            json={"on": True, "apps": ["rm -rf"]}).json()["message"] == "That doesn't look like an app."
    avery = people["phones"]["Avery"]
    assert avery.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=avery.h, json={"on": False}).status_code == 403

    off = dylan.client.put(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h, json={"on": False})
    assert off.json()["on"] is False
    assert _config(home_client, key, ts + 1).json()["launcher"] == {"on": False}


def test_the_tv_lists_its_apps_and_the_admin_card_picks_them(hub, people, home_client):
    _key, headers = _tv(hub, home_client)
    sent = home_client.post("/v1/tv/apps/installed", headers=headers, json={"apps": [
        {"package": "com.google.android.youtube.tv", "label": "YouTube"},
        {"package": "com.google.android.youtube.tvkids", "label": "YouTube Kids"},
        {"package": "not a package", "label": "Bad"},
        {"package": "com.netflix.ninja", "label": "Netflix"},
    ]})
    assert sent.json() == {"ok": True, "count": 3}
    dylan = people["phones"]["Dylan"]
    assert dylan.client.post("/v1/tv/apps/installed", headers=dylan.h, json={"apps": []}).status_code == 403
    got = dylan.client.get(f"/v1/tvapp/tvs/{TV_ID}/launcher", headers=dylan.h).json()
    assert [a["label"] for a in got["installed"]] == ["Netflix", "YouTube", "YouTube Kids"]
    assert got["on"] is False and got["box"] is None

    home_client.post("/v1/tv/fleet/report", headers=headers, json=BODY)
    csrf = _sign_in(home_client, people)
    page = home_client.get(f"/admin/devices/{TV_ID}").text
    assert "<h2>Launcher</h2>" in page and "Kids launcher" not in page and "YouTube Kids" in page and "Theo (child)" in page
    saved = home_client.post(f"/admin/devices/{TV_ID}/launcher", data={
        "csrf": csrf, "on": "1", "person_id": people["theo"]["id"], "guard": "1",
        "app": ["com.google.android.youtube.tvkids", "com.google.android.youtube.tv"],
    }, follow_redirects=False)
    assert saved.status_code == 303 and "Saved" in saved.headers["location"]
    block = tvlauncher.view(hub.tv_app.tvs()[TV_ID])
    assert block["on"] and block["apps"] == ["com.google.android.youtube.tvkids", "com.google.android.youtube.tv"]
    # Without a box on the hub's list, the TV asks on screen and the card says so.
    assert "asks once" in home_client.get(f"/admin/devices/{TV_ID}").text


async def test_the_box_gets_home_and_guard_and_gives_them_back(hub, people, home_client):
    _tv(hub, home_client)
    adb = ScriptAdb(_replies(**{"shell settings get": "com.other/.Reader"}))
    hub.tv_app.adb = adb
    boxes = hub.tv_app.boxes()
    boxes["10.0.0.86"] = {"name": "Theo's TV", "auto": True}
    hub.tv_app._save_boxes(boxes)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["ip"] = "10.0.0.86"
    hub.tv_app._save_tvs(tvs)
    tvlauncher.set_launcher(hub, TV_ID, {"on": True, "person_id": people["theo"]["id"],
                                         "apps": ["com.google.android.youtube.tvkids"], "guard": True}, apply=False)

    async def home_after_set(args, timeout):
        adb.calls.append(args)
        joined = " ".join(args)
        if "query-activities" in joined:
            return tvlauncher_result(f"com.google.android.tvlauncher/.MainActivity\n{ALIAS}")
        if "resolve-activity" in joined:
            seen = any("set-home-activity" in " ".join(c) for c in adb.calls)
            return tvlauncher_result(ALIAS if seen else "com.google.android.tvlauncher/.MainActivity")
        if "settings get secure enabled_accessibility_services" in joined:
            return tvlauncher_result(services[0])
        if "settings put secure enabled_accessibility_services" in joined:
            services[0] = args[-1]
        return await ScriptAdb._fake(adb, args, timeout)

    services = ["com.other/.Reader"]
    adb._run = home_after_set
    state = await tvlauncher.apply_box(hub, TV_ID)
    assert state["home"] is True and state["guard"] is True, state
    assert state["previous"] == "com.google.android.tvlauncher/.MainActivity"
    lines = [" ".join(c[2:]) for c in adb.calls if c[0] == "-s"]
    assert f"shell cmd package set-home-activity {ALIAS}" in lines
    assert "shell appops set me.dlewis.thomascreek.tv GET_USAGE_STATS allow" in lines
    assert f"shell settings put secure enabled_accessibility_services com.other/.Reader:{GUARD}" in lines

    adb.calls.clear()
    tvlauncher.set_launcher(hub, TV_ID, {"on": False}, apply=False)
    back = await tvlauncher.apply_box(hub, TV_ID)
    assert back["home"] is False and back["note"] == "The previous home screen is back."
    lines = [" ".join(c[2:]) for c in adb.calls if c[0] == "-s"]
    assert "shell cmd package set-home-activity com.google.android.tvlauncher/.MainActivity" in lines
    assert not any("pm disable" in line or "pm enable" in line for line in lines)
    assert "shell settings put secure enabled_accessibility_services com.other/.Reader" in lines


async def test_a_tv_that_never_enables_its_alias_keeps_the_guard(hub, people, home_client, monkeypatch):
    """Android 10+ refuses `pm enable` from the shell; without the app's own alias the hub only sets the guard."""
    _tv(hub, home_client)
    monkeypatch.setattr(tvlauncher, "ALIAS_WAIT_S", 0.0)
    adb = ScriptAdb(_replies(**{"shell settings get": ""}))
    hub.tv_app.adb = adb
    boxes = hub.tv_app.boxes()
    boxes["10.0.0.86"] = {"name": "Office TV", "auto": True}
    hub.tv_app._save_boxes(boxes)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["ip"] = "10.0.0.86"
    hub.tv_app._save_tvs(tvs)
    tvlauncher.set_launcher(hub, TV_ID, {"on": True}, apply=False)
    services = [""]

    async def no_alias(args, limit):
        adb.calls.append(args)
        joined = " ".join(args)
        if "query-activities" in joined:
            return tvlauncher_result("com.google.android.apps.tv.launcherx/.home.HomeActivity")
        if "resolve-activity" in joined:
            return tvlauncher_result("com.google.android.apps.tv.launcherx/.home.HomeActivity")
        if "settings get secure enabled_accessibility_services" in joined:
            return tvlauncher_result(services[0])
        if "settings put secure enabled_accessibility_services" in joined:
            services[0] = args[-1]
        return await ScriptAdb._fake(adb, args, limit)

    adb._run = no_alias
    state = await tvlauncher.apply_box(hub, TV_ID)
    assert state["home"] is False and state["guard"] is True, state
    assert "keeps its own Home button" in state["note"]
    lines = [" ".join(c[2:]) for c in adb.calls if c[0] == "-s"]
    assert not any("set-home-activity" in line for line in lines)


def tvlauncher_result(text: str):
    from hub.tvapp import AdbResult

    return AdbResult(0, text)
