"""TV app settings live on the hub: the shared keys both TVs use, each TV's own app settings, a person's own TV
settings, the schema the editors draw, copying a setup from one TV to another, and the admin page card."""

from __future__ import annotations

from hub import tvs

from tests.test_tv_api import KID_TV, TV_ID, Clock, Key, _auth, _config, _enroll, _token
from tests.test_tv_downloads import _sign_in
from tests.test_tv_fleet import BODY


def _tv(hub, home_client, key: Key | None = None):
    key = key or Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    token = _token(home_client, key, Clock())["access_token"]
    return key, _auth(token)


def _second(hub, home_client):
    key = Key()
    _enroll(home_client, key, KID_TV, "Basement")
    hub.tv_app.set_tv(KID_TV, status="approved", room="basement")
    return key


ANDROID = {
    "ipvio_general/startTab": ["i", 3],
    "ipvio_settings/seekStep": ["i", 10],
    "ipvio_side/labels": ["b", True],
    "ipvio_hidecats/cats": ["t", ["Adult", "Sports"]],
}


def test_the_tv_keeps_its_shared_and_own_settings_on_the_hub(hub, home_client, people):
    key, headers = _tv(hub, home_client)
    pushed = home_client.post("/v1/tv/settings", headers=headers, json={"settings": {
        "start_page": "live",
        "menu": {"order": ["home", "live", "movies", "series", "settings"], "hidden": ["search", "guide"]},
        "look": {"sidebar_labels": True, "motion": False},
        "startup": {"autostart": True, "resume_live": False, "resume_vod": True, "confirm_exit": True},
        "phone_page": False,
        "device": {"android": ANDROID},
    }})
    assert pushed.status_code == 200, pushed.text
    held = pushed.json()["settings"]
    assert held["start_page"] == "live" and held["menu"]["hidden"] == ["search", "guide"]
    assert held["device"]["android"]["ipvio_settings/seekStep"] == ["i", 10]

    body = _config(home_client, key, Clock().next()).json()
    assert body["settings"]["look"] == {"sidebar_labels": True, "motion": False}
    assert body["settings"]["device"]["android"] == ANDROID
    assert body["settings_by"] == "tv" and body["settings_rev"] == 1
    assert body["person_settings"] == {}

    dylan = people["phones"]["Dylan"]
    one = dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()
    assert one["settings_by"] == "tv" and one["settings_at"] and one["settings"]["phone_page"] is False


def test_bad_settings_are_refused_with_a_reason(hub, home_client, people):
    _key, headers = _tv(hub, home_client)
    for bad, message in (
        ({"start_page": "sport"}, "start_page is one of home, movies, series, live, search."),
        ({"menu": {"hidden": ["settings"]}}, "Home and Settings always stay in the menu."),
        ({"menu": {"hidden": ["casino"]}}, "menu.hidden uses home, live, movies, series, sport, guide, favorites, mylist, history, search, settings."),
        ({"menu": {"pinned": []}}, "menu takes order and hidden."),
        ({"look": {"sidebar_labels": "yes"}}, "look.sidebar_labels is true or false."),
        ({"look": {"accent": True}}, "look takes sidebar_labels, motion."),
        ({"startup": {"autostart": 1}}, "startup.autostart is true or false."),
        ({"phone_page": "on"}, "phone_page is true or false."),
        ({"device": {"roku": {}}}, "device has android and appletv."),
        ({"device": {"android": {"bad key!": 1}}}, "device.android has a name the hub can't use."),
        ({"device": {"android": {"x": {"nested": 1}}}}, "A device setting is a plain value or a list of them."),
        ({"device": {"android": {"x": "y" * 5000}}}, "A device setting is at most 4000 characters."),
    ):
        refused = home_client.post("/v1/tv/settings", headers=headers, json={"settings": bad})
        assert refused.status_code == 400, bad
        assert refused.json()["message"] == message, refused.text
    too_many = {"device": {"android": {f"k{i}": i for i in range(tvs.DEVICE_MAX_KEYS + 1)}}}
    assert home_client.post("/v1/tv/settings", headers=headers, json={"settings": too_many}).status_code == 400
    assert hub.tv_app.tvs()[TV_ID].get("settings_rev") in (None, 0)


def test_the_schema_is_one_editor_for_phone_and_admin_page(hub, home_client, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    got = dylan.client.get("/v1/tvs/settings/schema", headers=dylan.h)
    assert got.status_code == 200, got.text
    keys = [f["key"] for f in got.json()["settings"]]
    assert "start_page" in keys and "phone_page" in keys and "menu.hidden" in keys
    assert all(f["scope"] in ("tv", "person") for f in got.json()["settings"])
    assert "settings" not in next(f for f in got.json()["settings"] if f["key"] == "menu.hidden")["choices"]
    assert set(got.json()["personal_keys"]) == set(tvs.PERSONAL_KEYS)
    assert theo.client.get("/v1/tvs/settings/schema", headers=theo.h).status_code == 403


def test_a_person_carries_their_tv_settings_to_every_tv(hub, home_client, people):
    key, _headers = _tv(hub, home_client)
    clock = Clock()
    tag = _config(home_client, key, clock.next()).headers["etag"]
    dylan = people["phones"]["Dylan"]
    saved = dylan.client.patch("/v1/me/settings", headers=dylan.h, json={"changes": {
        "tv.start_page": "movies", "tv.player": {"subtitles": "auto"}, "tv.look": {"motion": False}}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["tv.start_page"] == "movies"

    pulled = _config(home_client, key, clock.next(), {"If-None-Match": tag})
    assert pulled.status_code == 200  # the person's change made every TV pull again
    layer = pulled.json()["person_settings"][people["dylan"]["id"]]
    assert layer == {"start_page": "movies", "player": {"subtitles": "auto"}, "look": {"motion": False}}

    # Device-wide settings are not a person's to carry; bad values are refused.
    for bad in ({"tv.parental": {"locked": False}}, {"tv.phone_page": True}, {"tv.start_page": "casino"}):
        refused = dylan.client.patch("/v1/me/settings", headers=dylan.h, json={"changes": bad})
        assert refused.status_code == 400, refused.text

    # null removes it again
    dylan.client.patch("/v1/me/settings", headers=dylan.h, json={"changes": {"tv.start_page": None}})
    layer = _config(home_client, key, clock.next()).json()["person_settings"][people["dylan"]["id"]]
    assert "start_page" not in layer


def test_a_new_tv_copies_the_setup_of_another(hub, home_client, people):
    _key, headers = _tv(hub, home_client)
    _second(hub, home_client)
    dylan = people["phones"]["Dylan"]
    assert dylan.client.post(f"/v1/tvs/{KID_TV}/settings/copy", headers=dylan.h,
                             json={"from": TV_ID}).json()["message"] == "That TV hasn't saved any settings on the hub yet."
    home_client.post("/v1/tv/settings", headers=headers, json={"settings": {
        "start_page": "live", "device": {"android": ANDROID}}})
    dylan.client.patch(f"/v1/tvs/{KID_TV}/settings", headers=dylan.h, json={"settings": {"language": "en"}})

    copied = dylan.client.post(f"/v1/tvs/{KID_TV}/settings/copy", headers=dylan.h, json={"from": TV_ID})
    assert copied.status_code == 200, copied.text
    assert copied.json()["settings"] == {"start_page": "live", "device": {"android": ANDROID}}
    assert hub.tv_app.tvs()[KID_TV]["settings_by"] == "phone"
    assert dylan.client.post(f"/v1/tvs/{KID_TV}/settings/copy", headers=dylan.h, json={"from": KID_TV}).status_code == 400
    assert dylan.client.post(f"/v1/tvs/{KID_TV}/settings/copy", headers=dylan.h, json={"from": "tv_nope"}).status_code == 404
    theo = people["phones"]["Theo"]
    assert theo.client.post(f"/v1/tvs/{KID_TV}/settings/copy", headers=theo.h, json={"from": TV_ID}).status_code == 403


def test_the_admin_page_edits_and_copies_settings(hub, home_client, people):
    _key, headers = _tv(hub, home_client)
    _second(hub, home_client)
    home_client.post("/v1/tv/settings", headers=headers, json={"settings": {
        "startup": {"autostart": True}, "device": {"android": ANDROID}}})
    home_client.post("/v1/tv/fleet/report", headers=headers, json=BODY)
    csrf = _sign_in(home_client, people)
    page = home_client.get(f"/admin/devices/{TV_ID}?tab=settings").text
    assert "<h2>Settings</h2>" in page and "Opens on" in page and "android 4" in page

    saved = home_client.post(f"/admin/devices/{TV_ID}/settings", data={
        "csrf": csrf, "start_page": "movies", "startup.autostart": "1", "look.motion": "1",
        "menu.hidden": ["search", "guide"]}, follow_redirects=False)
    assert saved.status_code == 303 and "Saved" in saved.headers["location"], saved.headers["location"]
    held = tvs.settings_of(hub.tv_app.tvs()[TV_ID])
    assert held["start_page"] == "movies" and held["look"] == {"motion": True}
    assert held["menu"] == {"hidden": ["search", "guide"]} and held["startup"] == {"autostart": True}
    # Fields never set stay unset (the TV keeps its own default), and the device set is untouched.
    assert "player" not in held and "phone_page" not in held and held["device"]["android"] == ANDROID
    assert hub.tv_app.tvs()[TV_ID]["settings_by"] == "admin"

    unticked = home_client.post(f"/admin/devices/{TV_ID}/settings", data={"csrf": csrf, "start_page": "movies",
                                                                        "look.motion": "1", "menu.hidden": ["search", "guide"]},
                                follow_redirects=False)
    assert "Saved" in unticked.headers["location"]
    assert tvs.settings_of(hub.tv_app.tvs()[TV_ID])["startup"] == {"autostart": False}

    copied = home_client.post(f"/admin/devices/{KID_TV}/settings", data={"csrf": csrf, "copy_from": TV_ID},
                              follow_redirects=False)
    assert "Copied" in copied.headers["location"]
    assert tvs.settings_of(hub.tv_app.tvs()[KID_TV])["start_page"] == "movies"
