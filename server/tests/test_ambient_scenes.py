"""YouTube ambiance scenes: categories filled by the hub, parents add and remove, clips loop on the TVs."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from hub.ambient_scenes import CATEGORIES, KV_CLIPS, season_category, video_id
from hub.tvfleet import report

from tests.test_tv_fleet import BODY
from tests.test_tv_api import TV_ID
from tests.test_tvs import _drain, _screen, _tv

TZ = ZoneInfo("America/New_York")


def _result(vid: str, title: str, minutes: int = 180, **extra) -> dict:
    return {"id": vid, "title": title, "duration": minutes * 60, "channel": "Cozy Channel", "channel_id": "UCcozy",
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/{vid}/hq720.jpg", "width": 720, "height": 404}], **extra}


def _fake_youtube(hub, results: dict[str, list[dict]]):
    """Search pages answer from `results` by the category's words; anything else answers nothing."""
    calls: list[str] = []

    async def flat(url: str, n: int, timeout: float = 60) -> dict:
        calls.append(url)
        for words, rows in results.items():
            if words in url:
                return {"entries": rows}
        return {"entries": []}

    hub.youtube.find._flat = flat
    return calls


def _fill(hub, now: datetime) -> list[str]:
    cabin = [
        _result("cabinJazz01", "Christmas Cabin Jazz by the Fire 4K"),
        _result("cabinJazz02", "Snowy Cabin Porch Ambience"),
        _result("shortClip03", "Cabin tour", minutes=4),                    # too short for a scene
        _result("liveCabin04", "Live cabin cam", live_status="is_live"),    # can't loop a live stream
        _result("sexyCabin05", "Sexy cabin night"),                         # never shown
        _result("cabinJazz06", "Holiday Cabin Night"),
    ]
    calls = _fake_youtube(hub, {"christmas+cabin": cabin, "crackling+fireplace": [_result("fireplace01", "Crackling Fireplace 10 hours")]})
    asyncio.run(hub.ambient.scenes.refresh(now))
    return calls


def test_categories_fill_from_youtube_and_the_season_comes_first(hub, people):
    hub.config.ambient.save = False   # no clips: everything opens in the YouTube app
    december = datetime(2026, 12, 18, 20, tzinfo=TZ)
    calls = _fill(hub, december)
    assert len(calls) == len(CATEGORIES) and all("sp=EgIQAQ" in url for url in calls)
    assert season_category(december) == "holiday-cabin"
    assert season_category(datetime(2026, 7, 4, tzinfo=TZ)) == "forest"
    assert season_category(datetime(2026, 10, 30, tzinfo=TZ)) == "halloween"

    page = hub.ambient.scenes.page(None, child=False, can_edit=True, now=december)
    first = page["categories"][0]
    assert first["id"] == "holiday-cabin" and page["reason"] == "For the holidays"
    assert [i["id"] for i in first["items"]] == ["cabinJazz01", "cabinJazz02", "cabinJazz06"]
    item = first["items"][0]
    assert item["state"] == "online" and "stream_url" not in item
    assert item["youtube_url"] == "https://www.youtube.com/watch?v=cabinJazz01"
    assert item["picture_url"].startswith("/v1/youtube/thumb/") and "ytimg" not in item["picture_url"]
    assert item["duration_s"] == 10800 and item["channel"] == "Cozy Channel"
    assert page["featured"]["id"] == "cabinJazz01"
    assert [c["id"] for c in page["categories"]] == ["holiday-cabin", "fireplace"]   # empty searches stay out
    assert page["idle"] == {"enabled": True, "after_min": 10, "sound": False, "choice": "season"}

    # A second refresh inside the window asks YouTube nothing.
    again: list[str] = []
    hub.youtube.find._flat = lambda url, n, timeout=60: again.append(url)
    asyncio.run(hub.ambient.scenes.refresh(december))
    assert again == []


def test_parents_add_and_remove_and_children_cannot(hub, people, monkeypatch):
    hub.config.ambient.save = False
    december = datetime(2026, 12, 18, 20, tzinfo=TZ)
    _fill(hub, december)
    dylan, avery, theo = (people["phones"][n] for n in ("Dylan", "Avery", "Theo"))

    async def lookup(vid):
        return {"title": "Coffee Shop Jazz Rainy Night", "channel": "Cafe", "duration_s": 7200}

    monkeypatch.setattr(hub.ambient.scenes, "_lookup", lookup)
    added = avery.client.post("/v1/ambient/scenes", headers=avery.h,
                              json={"video": "https://youtu.be/cafeJazz123?si=x", "category": "cafe"})
    assert added.status_code == 200, added.text
    assert added.json()["id"] == "cafeJazz123" and added.json()["added"] is True
    assert theo.client.post("/v1/ambient/scenes", headers=theo.h,
                            json={"video": "cafeJazz123", "category": "cafe"}).status_code == 403
    assert dylan.client.post("/v1/ambient/scenes", headers=dylan.h,
                             json={"video": "not a link", "category": "cafe"}).json()["error"] == "bad_link"
    assert dylan.client.post("/v1/ambient/scenes", headers=dylan.h,
                             json={"video": "cafeJazz123", "category": "disco"}).status_code == 400

    page = theo.client.get("/v1/ambient/scenes", headers=theo.h).json()
    assert page["can_edit"] is False
    cafe = next(c for c in page["categories"] if c["id"] == "cafe")
    assert cafe["items"][0]["title"] == "Coffee Shop Jazz Rainy Night"
    assert "sexyCabin05" not in str(page)
    assert theo.client.get("/v1/ambient/search", headers=theo.h, params={"q": "jazz"}).status_code == 403

    gone = dylan.client.delete("/v1/ambient/scenes/cabinJazz01", headers=dylan.h)
    assert gone.status_code == 200
    asyncio.run(hub.ambient.scenes.refresh(december))
    page = dylan.client.get("/v1/ambient/scenes", headers=dylan.h).json()
    assert page["can_edit"] is True
    assert "cabinJazz01" not in str(page)                 # a removed suggestion stays removed

    found = dylan.client.get("/v1/ambient/search", headers=dylan.h, params={"q": "https://www.youtube.com/watch?v=cafeJazz123"})
    assert found.status_code == 200 and found.json()["items"][0]["id"] == "cafeJazz123"
    assert video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ" and video_id("https://example.com/x") is None


def test_a_saved_clip_loops_on_the_idle_screen(hub, people, tmp_path):
    hub.youtube.root = tmp_path / "youtube"
    hub.config.ambient.save = True
    december = datetime(2026, 12, 18, 20, tzinfo=TZ)
    _fill(hub, december)
    scenes = hub.ambient.scenes
    clips = hub.db.kv_get(KV_CLIPS)
    assert sorted(clips) == ["cabinJazz01", "cabinJazz02"]       # the season's top two are saved on their own
    assert {c["status"] for c in clips.values()} == {"queued"}
    assert scenes.idle_scene(None, december) is None            # nothing to loop yet
    assert "video" not in hub.ambient.view(None, december)

    async def fake_save(vid):
        scenes.root.mkdir(parents=True, exist_ok=True)
        (scenes.root / f"{vid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        (scenes.root / f"{vid}.jpg").write_bytes(b"\xff\xd8pic")
        scenes._set_clip(vid, status="ready", bytes=80, ready_at=time.time())

    scenes.save = fake_save
    while asyncio.run(scenes.save_next()):
        pass
    view = hub.ambient.view(None, december)
    assert view["video"]["id"] == "cabinJazz01" and view["video"]["stream_url"] == "/v1/ambient/videos/cabinJazz01.mp4"
    assert view["video"]["picture_url"] == "/v1/ambient/pictures/cabinJazz01.jpg"
    assert view["idle"]["after_min"] == 10 and view["ambience"]["id"] == "christmas"   # the old fields stay

    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    clip = theo.client.get("/v1/ambient/videos/cabinJazz01.mp4", headers={**theo.h, "Range": "bytes=0-7"})
    assert clip.status_code == 206 and clip.content == b"\x00\x00\x00\x18ftyp"
    assert dylan.client.get("/v1/ambient/videos/fireplace01.mp4", headers=dylan.h).status_code == 404
    assert dylan.client.get("/v1/ambient/videos/..%2Fhub.mp4", headers=dylan.h).status_code == 404

    # Idle settings: house-wide, then one TV picks its own scene and waits longer.
    assert theo.client.put("/v1/ambient/idle", headers=theo.h, json={"after_min": 1}).status_code == 403
    assert dylan.client.put("/v1/ambient/idle", headers=dylan.h, json={"after_min": 0}).status_code == 400
    assert dylan.client.put("/v1/ambient/idle", headers=dylan.h, json={"choice": "nope"}).status_code == 400
    house = dylan.client.put("/v1/ambient/idle", headers=dylan.h, json={"after_min": 5, "sound": True})
    assert house.json() == {"enabled": True, "after_min": 5, "sound": True, "choice": "season"}
    den = dylan.client.put("/v1/ambient/idle", headers=dylan.h,
                           json={"device": "den", "choice": "cabinJazz02", "after_min": 20}).json()
    assert den == {"enabled": True, "after_min": 20, "sound": True, "choice": "cabinJazz02"}
    assert hub.ambient.view("den", december)["video"]["id"] == "cabinJazz02"
    assert hub.ambient.view(None, december)["video"]["id"] == "cabinJazz01"
    dylan.client.put("/v1/ambient/idle", headers=dylan.h, json={"enabled": False})
    assert "video" not in hub.ambient.view(None, december)

    # A press on an unsaved scene asks for it; the TV opens YouTube meanwhile.
    want = theo.client.post("/v1/ambient/scenes/fireplace01/save", headers=theo.h)
    assert want.json()["state"] == "queued"
    assert theo.client.post("/v1/ambient/scenes/zzzzzzzzzzz/save", headers=theo.h).status_code == 404

    # Removing a scene deletes its clip.
    dylan.client.delete("/v1/ambient/scenes/cabinJazz02", headers=dylan.h)
    assert not (scenes.root / "cabinJazz02.mp4").exists()
    assert hub.ambient.scenes.idle("den")["choice"] == "season"


def test_trim_keeps_added_scenes(hub, tmp_path):
    hub.youtube.root = tmp_path / "youtube"
    scenes = hub.ambient.scenes
    scenes.root.mkdir(parents=True)
    hub.config.ambient.cap_gb = 1
    hub.db.kv_set("tv.ambient.scenes", {"added": {"keepMe12345": {"id": "keepMe12345", "category": "cafe"}}})
    for vid, played in (("keepMe12345", 1), ("oldOne12345", 2), ("newOne12345", 3)):
        (scenes.root / f"{vid}.mp4").write_bytes(b"0")
        scenes._set_clip(vid, status="ready", bytes=600_000_000, played_at=played)
    assert scenes.trim() == 2
    assert sorted(hub.db.kv_get(KV_CLIPS)) == ["keepMe12345"]


def test_a_phone_plays_a_scene_on_a_tv(hub, home_client, people):
    hub.config.ambient.save = False
    _fill(hub, datetime(2026, 12, 18, 20, tzinfo=TZ))
    _tv(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    screen = _screen(hub)
    dylan = people["phones"]["Dylan"]
    play = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h,
                             json={"action": "play", "ambience_id": "cabinJazz02"})
    assert play.status_code == 200, play.text
    sent = _drain(screen.queue)[0]["data"]
    assert sent["kind"] == "ambience" and sent["ambience_id"] == "cabinJazz02"
    assert sent["title"] == "Snowy Cabin Porch Ambience"
    missing = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h,
                                json={"action": "play", "ambience_id": "zzzzzzzzzzz"})
    assert missing.status_code == 404
