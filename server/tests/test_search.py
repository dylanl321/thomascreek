"""GET /v1/search: one search over the house, limited by who is asking."""

from __future__ import annotations

import asyncio

from hub import search as engine


def _types(body):
    return {g["type"]: g for g in body["groups"]}


def _find(body, title):
    for g in body["groups"]:
        for i in g["items"]:
            if i["title"] == title:
                return i
    return None


def test_admin_finds_room_and_light_with_actions(hub, people, home_client):
    ph = people["phones"]["Dylan"]
    r = home_client.get("/v1/search", params={"q": "kitchen"}, headers=ph.h)
    assert r.status_code == 200, r.text
    body = r.json()
    room = _find(body, "Kitchen") if _types(body).get("room") else None
    assert room and room["type"] == "room" and room["deep_link"] == "thomascreek://room/kitchen"
    assert room["action"]["action"] == "batch" and room["action"]["label"] == "All off"
    assert room["action"]["steps"] == [{"action": "light.set", "target": "light.kitchen", "params": {"on": False},
                                       "tier": "everyday", "toggle": True, "on": True}] or \
        room["action"]["steps"][0]["target"] == "light.kitchen"
    light = next(i for i in _types(body)["light"]["items"] if i["ref"] == "light.kitchen")
    assert light["action"]["action"] == "light.set" and light["action"]["params"] == {"on": False}
    assert "on, 50%" in light["subtitle"]


def test_child_sees_only_own_things(hub, people, home_client):
    theo = people["phones"]["Theo"]
    garage = home_client.get("/v1/search", params={"q": "garage"}, headers=theo.h).json()
    assert not any(g["type"] == "door" for g in garage["groups"])
    kitchen = home_client.get("/v1/search", params={"q": "kitchen"}, headers=theo.h).json()
    assert kitchen["groups"] == [] or all(g["type"] in ("screen",) for g in kitchen["groups"])
    lamp = home_client.get("/v1/search", params={"q": "theo lamp"}, headers=theo.h).json()
    hit = _find(lamp, "Theo's lamp")
    assert hit and hit["action"]["action"] == "light.set"
    vpn = home_client.get("/v1/search", params={"q": "vpn"}, headers=theo.h).json()
    assert not any(i["id"] == "screen:vpn" for g in vpn["groups"] for i in g["items"])


def test_garage_open_is_secure_tier(hub, people, home_client):
    ph = people["phones"]["Dylan"]
    body = home_client.get("/v1/search", params={"q": "garage"}, headers=ph.h).json()
    door = _find(body, "Large garage door")
    assert door["action"] == {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True},
                              "tier": "secure", "label": "Open"}


def test_command_turns_a_room_off(hub, people, home_client):
    ph = people["phones"]["Avery"]
    body = home_client.get("/v1/search", params={"q": "kitchen off"}, headers=ph.h).json()
    cmd = body["command"]
    assert cmd["title"] == "Turn off Kitchen"
    assert [s["target"] for s in cmd["action"]["steps"]] == ["light.kitchen"]
    # Each step runs through /v1/actions unchanged.
    step = cmd["action"]["steps"][0]
    r = home_client.post("/v1/actions", json={"action": step["action"], "target": step["target"],
                                              "params": step["params"]}, headers=ph.h)
    assert r.status_code == 200, r.text


def test_scene_and_screens(hub, people, home_client):
    ph = people["phones"]["Dylan"]
    body = home_client.get("/v1/search", params={"q": "movie"}, headers=ph.h).json()
    scene = _find(body, "Movie night")
    assert scene["action"]["action"] == "scene.run" and scene["verb"] == "Run"
    hubweb = home_client.get("/v1/search", params={"q": "hub web"}, headers=ph.h).json()
    assert _find(hubweb, "Hub web page")["deep_link"] == "thomascreek://hubweb"
    avery = people["phones"]["Avery"]
    hubweb2 = home_client.get("/v1/search", params={"q": "hub web"}, headers=avery.h).json()
    assert _find(hubweb2, "Hub web page") is None


def test_plex_rated_for_children_and_slow_sources_pending(hub, people, home_client, monkeypatch):
    hub.config.plex.url = "http://plex.test"
    hub.config.plex.enabled = True

    async def fake(q, limit=30):
        return [{"id": "1", "title": "Bluey", "kind": "show", "rating": "TV-Y", "year": 2018},
                {"id": "2", "title": "Bluey Grown Up", "kind": "movie", "rating": "R", "year": 2030}]

    monkeypatch.setattr(hub.plex, "search", fake)
    theo = people["phones"]["Theo"]
    body = home_client.get("/v1/search", params={"q": "bluey"}, headers=theo.h).json()
    titles = [i["title"] for i in _types(body)["media"]["items"]]
    assert titles == ["Bluey"]
    assert _types(body)["media"]["items"][0]["media"]["id"] == "1"
    dylan = people["phones"]["Dylan"]
    body = home_client.get("/v1/search", params={"q": "bluey"}, headers=dylan.h).json()
    assert len(_types(body)["media"]["items"]) == 2

    async def slow(q, limit=30):
        await asyncio.sleep(3)
        return []

    monkeypatch.setattr(hub.plex, "search", slow)
    monkeypatch.setattr(engine, "BUDGET_S", 0.05)
    engine._cache.clear()
    r = home_client.get("/v1/search", params={"q": "something else"}, headers=dylan.h)
    assert r.status_code == 200
    assert "media" in r.json()["pending"]


def test_matching_and_questions():
    ws = engine.words("Basement calm")
    assert engine.score(ws, "Basement Calm") > engine.score(ws, "Basement Main Room Calm Lights")
    assert engine.score(engine.words("bath"), "Basement Bathroom") > 0
    assert engine.score(engine.words("lights"), "Kitchen light") > 0
    assert engine.score(engine.words("xyz"), "Kitchen") == 0
    assert engine.looks_like_question("is the garage open")
    assert not engine.looks_like_question("garage")


def test_ghost_entities_are_hidden(hub, people, home_client):
    hub.home.states["media_player.plex_cast"] = {"entity_id": "media_player.plex_cast", "state": "unavailable",
                                                 "attributes": {"friendly_name": "Plex (Plex Cast - Chromecast)"}}
    hub.home.states["light.0xa4c1"] = {"entity_id": "light.0xa4c1", "state": "unavailable",
                                       "attributes": {"friendly_name": "0xa4c138a3d254e82e"}}
    hub.home.states["light.kitchen_seg1"] = {"entity_id": "light.kitchen_seg1", "state": "on",
                                             "attributes": {"friendly_name": "Kitchen Segment 1"}}
    hub.home.entity_area["light.kitchen_seg1"] = "kitchen"
    ph = people["phones"]["Dylan"]
    for q in ("plex", "0xa4c138a3d254e82e", "segment"):
        body = home_client.get("/v1/search", params={"q": q}, headers=ph.h).json()
        assert not any(g["type"] in ("light", "player") for g in body["groups"]), (q, body)


def test_pending_phone_is_refused(hub, home_client):
    from tests.conftest import Phone

    ph = Phone(home_client)
    ph.enroll()
    ph.sign_in()
    assert home_client.get("/v1/search", params={"q": "x"}, headers=ph.h).status_code == 403


def test_youtube_videos_are_found(hub, people, home_client):
    import time

    hub.config.youtube.enabled = True
    hub.db.insert("yt_videos", {"id": "abcdefghijk", "source_ids": [], "title": "How to draw a robot",
                                "channel": "Art for Kids Hub", "duration_s": 720, "status": "ready",
                                "added_at": time.time(), "ready_at": time.time()})
    theo = people["phones"]["Theo"]
    body = home_client.get("/v1/search", params={"q": "robot"}, headers=theo.h).json()
    hit = _find(body, "How to draw a robot")
    assert hit and hit["type"] == "youtube" and hit["media"]["id"] == "yt-abcdefghijk"
