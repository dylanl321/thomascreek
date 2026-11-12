"""Per-person settings and favorites, live sync, parents editing a child's, and View as."""

from __future__ import annotations

from .conftest import Phone


def test_settings_start_from_defaults_and_follow_the_person(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = home_client.get("/v1/me/settings", headers=dylan.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["settings"]["downloads.cellular"] is False and body["settings"]["appearance"] == "system"
    assert set(body["favorites"]) == {"room", "device", "camera", "mode", "media", "recipe", "youtube"}

    r = home_client.patch("/v1/me/settings", headers=dylan.h,
                          json={"changes": {"downloads.cellular": True, "home.order": ["favorites", "cameras", "rightnow"]}})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1

    # A second phone of Dylan's sees the same choices.
    ipad = Phone(home_client, "Dylan's iPad")
    ipad.enroll()
    hub.db.update("devices", "id", ipad.device_id, {"person_id": people["dylan"]["id"], "status": "active"})
    ipad.sign_in()
    s = home_client.get("/v1/me/settings", headers=ipad.h).json()
    assert s["settings"]["downloads.cellular"] is True
    assert s["settings"]["home.order"] == ["favorites", "cameras", "rightnow"]
    assert home_client.get("/v1/me", headers=ipad.h).json()["settings_version"] == 1

    # Someone else's are untouched.
    avery = home_client.get("/v1/me/settings", headers=people["phones"]["Avery"].h).json()
    assert avery["settings"]["downloads.cellular"] is False and avery["version"] == 0


def test_bad_settings_are_refused(people, home_client):
    h = people["phones"]["Dylan"].h
    assert home_client.patch("/v1/me/settings", headers=h, json={"changes": {"appearance": "neon"}}).status_code == 400
    assert home_client.patch("/v1/me/settings", headers=h, json={"changes": {"vpn.mode": "always"}}).json()["error"] == "unknown_setting"
    assert home_client.patch("/v1/me/settings", headers=h, json={"changes": {"home.order": ["nope"]}}).status_code == 400
    # Free-form app keys are allowed, within a size limit.
    assert home_client.patch("/v1/me/settings", headers=h, json={"changes": {"app.recipes.sort": "newest"}}).status_code == 200
    assert home_client.patch("/v1/me/settings", headers=h, json={"changes": {"app.big": "x" * 3000}}).status_code == 400


def test_stars_merge_and_reorder(people, home_client):
    h = people["phones"]["Dylan"].h
    for eid in ("light.kitchen", "lock.front_door", "cover.large_garage"):
        assert home_client.put(f"/v1/me/favorites/device/{eid}", headers=h).status_code == 200
    home_client.put("/v1/me/favorites/media/4242", headers=h, json={"title": "Planet Earth", "image": "/v1/media/art?id=4242"})
    r = home_client.delete("/v1/me/favorites/device/lock.front_door", headers=h)
    favs = r.json()["favorites"]
    assert [f["id"] for f in favs["device"]] == ["light.kitchen", "cover.large_garage"]
    assert favs["media"] == [{"id": "4242", "title": "Planet Earth", "image": "/v1/media/art?id=4242"}]

    r = home_client.post("/v1/me/favorites/device/order", headers=h, json={"ids": ["cover.large_garage", "light.kitchen"]})
    assert [f["id"] for f in r.json()["favorites"]["device"]] == ["cover.large_garage", "light.kitchen"]
    # A stale order from another device keeps a star it didn't know about.
    home_client.put("/v1/me/favorites/device/light.living_room", headers=h)
    r = home_client.post("/v1/me/favorites/device/order", headers=h, json={"ids": ["light.kitchen", "cover.large_garage"]})
    assert [f["id"] for f in r.json()["favorites"]["device"]] == ["light.kitchen", "cover.large_garage", "light.living_room"]
    # Starring twice is harmless; unknown kinds are not.
    assert home_client.put("/v1/me/favorites/device/light.kitchen", headers=h).status_code == 200
    assert home_client.put("/v1/me/favorites/spaceship/x", headers=h).status_code == 404


def test_children_can_only_star_what_they_can_use(people, home_client):
    h = people["phones"]["Theo"].h
    assert home_client.put("/v1/me/favorites/device/light.theo_room", headers=h).status_code == 200
    assert home_client.put("/v1/me/favorites/room/theo_room", headers=h).status_code == 200
    for path in ("device/lock.front_door", "device/cover.large_garage", "camera/driveway", "room/garage"):
        r = home_client.put(f"/v1/me/favorites/{path}", headers=h)
        assert r.status_code == 403, path
        assert r.json()["error"] == "not_allowed"


def test_room_stars_are_per_person_and_seeded_from_config(hub, people, home_client):
    hub.config.favorites = ["kitchen"]
    d, a = people["phones"]["Dylan"].h, people["phones"]["Avery"].h
    rooms = {r["id"]: r["favorite"] for r in home_client.get("/v1/home", headers=d).json()["rooms"]}
    assert rooms["kitchen"] is True and rooms["living_room"] is False
    home_client.put("/v1/me/favorites/room/living_room", headers=d)
    home_client.delete("/v1/me/favorites/room/kitchen", headers=d)
    mine = {r["id"]: r["favorite"] for r in home_client.get("/v1/home", headers=d).json()["rooms"]}
    theirs = {r["id"]: r["favorite"] for r in home_client.get("/v1/home", headers=a).json()["rooms"]}
    assert mine["living_room"] is True and mine["kitchen"] is False
    assert theirs["kitchen"] is True and theirs["living_room"] is False


def test_changes_reach_that_persons_other_devices_live(hub, people, home_client):
    got: list = []
    sub = hub.bus.subscribe(lambda ev: ev, people["dylan"]["id"])
    other = hub.bus.subscribe(lambda ev: ev, people["avery"]["id"])
    home_client.put("/v1/me/favorites/device/light.kitchen", headers=people["phones"]["Dylan"].h)
    while not sub.queue.empty():
        got.append(sub.queue.get_nowait())
    ev = next(e for e in got if e["type"] == "settings")
    assert ev["data"]["version"] == 1 and ev["data"]["device_id"] == people["phones"]["Dylan"].device_id
    assert all(e["type"] != "settings" for e in _drain(other))


def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_parents_edit_a_childs_favorites_but_not_the_other_way(people, home_client):
    theo_id = people["theo"]["id"]
    avery = people["phones"]["Avery"].h
    r = home_client.put(f"/v1/people/{theo_id}/favorites/device/light.theo_room", headers=avery)
    assert r.status_code == 200, r.text
    r = home_client.patch(f"/v1/people/{theo_id}/settings", headers=avery, json={"changes": {"home.order": ["favorites", "today"]}})
    assert r.status_code == 200
    theo = home_client.get("/v1/me/settings", headers=people["phones"]["Theo"].h).json()
    assert [f["id"] for f in theo["favorites"]["device"]] == ["light.theo_room"]
    assert theo["settings"]["home.order"] == ["favorites", "today"]
    # Even a parent can't give a child a star the child can't use.
    assert home_client.put(f"/v1/people/{theo_id}/favorites/device/lock.front_door", headers=avery).status_code == 403
    # An adult can't edit another adult's; a child can't edit anyone's.
    assert home_client.get(f"/v1/people/{people['dylan']['id']}/settings", headers=avery).status_code == 403
    assert home_client.get(f"/v1/people/{people['avery']['id']}/settings", headers=people["phones"]["Theo"].h).status_code == 403
    # Dylan can edit anyone's.
    assert home_client.get(f"/v1/people/{people['avery']['id']}/settings", headers=people["phones"]["Dylan"].h).status_code == 200


def test_migrate_keeps_what_another_device_already_sent(people, home_client):
    h = people["phones"]["Dylan"].h
    r = home_client.post("/v1/me/settings/migrate", headers=h, json={"local": {"downloads.quality": "small", "live_activity.auto": False}})
    assert r.json()["settings"]["downloads.quality"] == "small"
    r = home_client.post("/v1/me/settings/migrate", headers=h, json={"local": {"downloads.quality": "high", "vpn.mode": "always"}})
    assert r.json()["settings"]["downloads.quality"] == "small"
    assert r.json()["settings"]["live_activity.auto"] is False


def test_view_as_answers_as_that_person_and_changes_nothing(hub, people, home_client):
    hub.config.hub.dry_run = False
    dylan = people["phones"]["Dylan"]
    theo_id = people["theo"]["id"]
    view = {**dylan.h, "X-TC-View-As": theo_id}

    me = home_client.get("/v1/me", headers=view).json()
    assert me["role"] == "child" and me["person"]["name"] == "Theo"
    assert me["preview"] == {"person_id": theo_id, "by": people["dylan"]["id"]}
    snap = home_client.get("/v1/home", headers=view).json()
    assert {r["id"] for r in snap["rooms"]} <= {"theo_room", "living_room"}
    assert snap["cameras"] == []

    # Nothing in the house changes while previewing, even what Theo himself could do.
    r = home_client.post("/v1/actions", headers=view, json={"action": "light.set", "target": "light.theo_room", "params": {"on": True}})
    assert r.status_code == 403 and r.json()["error"] == "preview_only"
    assert hub.calls == []
    # His favorites and Home can be set up, and it's logged against Dylan.
    r = home_client.put("/v1/me/favorites/device/light.theo_room", headers=view)
    assert r.status_code == 200
    theo = home_client.get("/v1/me/settings", headers=people["phones"]["Theo"].h).json()
    assert [f["id"] for f in theo["favorites"]["device"]] == ["light.theo_room"]
    row = hub.db.one("SELECT * FROM activity WHERE action='settings.edit' ORDER BY id DESC")
    assert row["person_id"] == people["dylan"]["id"] and row["target"] == theo_id
    # Dylan's own favorites are untouched.
    assert home_client.get("/v1/me/settings", headers=dylan.h).json()["favorites"]["device"] == []

    assert home_client.post("/v1/me/view-as", headers=dylan.h, json={"person_id": theo_id}).status_code == 200
    assert home_client.get("/v1/me/view-as", headers=dylan.h).json()["people"][0]["name"] in ("Avery", "Theo")


def test_only_an_admin_can_view_as(people, home_client):
    avery = people["phones"]["Avery"]
    theo = people["phones"]["Theo"]
    r = home_client.get("/v1/me", headers={**avery.h, "X-TC-View-As": people["theo"]["id"]})
    assert r.status_code == 403
    r = home_client.get("/v1/me", headers={**theo.h, "X-TC-View-As": people["dylan"]["id"]})
    assert r.status_code == 403
    r = home_client.get("/v1/me", headers={**people["phones"]["Dylan"].h, "X-TC-View-As": "per_nobody"})
    assert r.status_code == 404
