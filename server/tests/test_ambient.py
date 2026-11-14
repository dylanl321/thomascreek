"""An idle TV picks a scene from the date, and uses a lineup channel when the name matches."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")


def _day(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 15, tzinfo=TZ)


def test_the_date_picks_the_scene_and_a_channel_name_is_enough(hub, people):
    hub.tv_app.set_lineup([
        {"name": "Autumn leaves", "source": "thomas-creek", "stream_id": "7"},
        {"name": "CNN", "source": "thomas-creek", "stream_id": "10"},
    ])
    dylan = people["phones"]["Dylan"]

    def scene(month: int, day: int, device: str | None = None) -> dict:
        return hub.ambient.view(device, _day(month, day))

    leaves = scene(11, 14)
    assert leaves["season"] == "autumn" and leaves["holiday"] is None
    assert leaves["ambience"]["id"] == "leaves"
    assert leaves["ambience"]["channel_key"] == "xc:thomas-creek:7"
    assert leaves["ambience"]["poster_url"].endswith("/xc:thomas-creek:7")
    assert "stream_url" not in leaves["ambience"]
    assert scene(10, 31)["holiday"] == "halloween" and scene(10, 31)["ambience"]["id"] == "halloween"
    assert scene(12, 20)["holiday"] == "christmas" and scene(12, 20)["ambience"]["title"] == "Christmas lights"
    assert scene(12, 28)["ambience"]["id"] == "fireplace"
    assert scene(1, 11)["ambience"]["id"] == "snowfall"
    assert scene(3, 2)["season"] == "spring" and scene(3, 2)["ambience"]["id"] == "rain"
    assert scene(7, 4)["season"] == "summer" and scene(7, 4)["ambience"]["id"] == "beach"

    saved = dylan.client.put("/v1/ambient", headers=dylan.h, json={
        "pin": "aquarium",
        "device": "living-room",
        "items": [
            {"id": "beach", "stream_url": "http://user:secret@cdn.example/beach"},
            {"id": "aquarium", "channel_key": "xc:thomas-creek:10"},
        ],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["items"][0]["id"] == "fireplace"
    beach = next(row for row in saved.json()["items"] if row["id"] == "beach")
    assert "stream_url" not in beach and "secret" not in saved.text
    pinned = scene(7, 4, "living-room")
    assert pinned["ambience"]["id"] == "aquarium"
    assert pinned["ambience"]["channel_key"] == "xc:thomas-creek:10"
    assert scene(7, 4)["ambience"]["id"] == "beach"
    assert people["phones"]["Avery"].client.put("/v1/ambient", headers=people["phones"]["Avery"].h,
                                                json={"pin": "rain"}).status_code == 403

    page = dylan.client.get("/v1/ambient", headers=dylan.h, params={"device": "living-room"}).json()
    assert page["ambience"]["title"] == "Aquarium"
    assert page["widgets"]["calendar"] == []
    assert isinstance(page["widgets"]["cameras"], list)


def test_a_camera_still_uses_the_tv_sign_in(hub, home_client, people):
    async def proxy(_entity):
        return b"\xff\xd8still"

    hub.ha.camera_proxy = proxy
    shot = people["phones"]["Dylan"].client.get("/v1/ambient/cameras/driveway.jpg", headers=people["phones"]["Dylan"].h)
    assert shot.status_code == 200 and shot.content == b"\xff\xd8still"
    assert "ha.test" not in shot.headers.get("content-type", "")
