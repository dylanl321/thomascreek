"""Kids' YouTube: links, adding without approval, subscriptions, remove/block, downloads like films."""

from __future__ import annotations

import asyncio

import pytest

from hub.youtube import YouTube, YouTubeError, _plain_error, parse_link


def test_parse_link():
    assert parse_link("https://youtu.be/dQw4w9WgXcQ?si=abc") == {
        "kind": "video", "id": "dQw4w9WgXcQ", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    assert parse_link("Check this out! https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghij")["kind"] == "video"
    assert parse_link("m.youtube.com/shorts/dQw4w9WgXcQ")["id"] == "dQw4w9WgXcQ"
    assert parse_link("https://www.youtube.com/playlist?list=PLabcdefghij123") == {
        "kind": "playlist", "id": "PLabcdefghij123", "url": "https://www.youtube.com/playlist?list=PLabcdefghij123"}
    assert parse_link("https://www.youtube.com/@MarkRober")["url"] == "https://www.youtube.com/@MarkRober/videos"
    assert parse_link("https://youtube.com/@MarkRober/shorts")["url"] == "https://www.youtube.com/@MarkRober/videos"
    assert parse_link("https://www.youtube.com/channel/UCY1kMZp36IQSyNx_9h4mpCg")["url"].endswith("/channel/UCY1kMZp36IQSyNx_9h4mpCg/videos")
    for bad in ("https://vimeo.com/1234", "https://www.youtube.com/feed/trending", "hello"):
        with pytest.raises(YouTubeError):
            parse_link(bad)


def test_plain_errors():
    assert "grown-ups" in _plain_error("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.")
    assert "private" in _plain_error("ERROR: [youtube] x: Private video. Sign in if you've been granted access")


def _entry(i: int, ch="UCmark", **kw):
    return {"id": f"vid{i:08d}", "title": f"Video {i}", "duration_s": 600, "channel": "Mark Rober", "channel_id": ch,
            "thumb": None, "uploaded": "20261101", **kw}


@pytest.fixture
def yt(hub, tmp_path, monkeypatch):
    hub.youtube = YouTube(hub, tmp_path / "youtube")
    feed = {"videos": [_entry(i) for i in range(3)]}

    async def lookup(link):
        return {"kind": link["kind"], "url": link["url"], "yt_id": "UCmark" if link["kind"] == "channel" else link["id"],
                "title": "Mark Rober" if link["kind"] == "channel" else "Video 0", "channel": "Mark Rober",
                "channel_id": "UCmark", "thumb": None, "followers": 1,
                "videos": list(feed["videos"])[:1] if link["kind"] == "video" else list(feed["videos"])}

    async def fetch(v):
        root = hub.youtube.root
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{v['id']}.mp4").write_bytes(b"x" * 1000)
        (root / f"{v['id']}.jpg").write_bytes(b"jpg")
        hub.db.update("yt_videos", "id", v["id"], {"status": "ready", "file": f"{v['id']}.mp4", "bytes": 1000,
                                                     "ready_at": v["added_at"], "height": 1080})

    monkeypatch.setattr(hub.youtube, "lookup", lookup)
    monkeypatch.setattr(hub.youtube, "fetch", fetch)
    hub.youtube.feed = feed
    return hub.youtube


def _drain(yt):
    async def go():
        while await yt.fetch_next():
            pass
    asyncio.run(go())


def test_child_adds_a_channel_without_approval_and_parents_hear(yt, people, hub):
    theo, dylan = people["phones"]["Theo"], people["phones"]["Dylan"]
    r = theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober", "follow": True}, headers=theo.h)
    assert r.status_code == 200, r.text
    src = r.json()
    assert src["kind"] == "channel" and src["follow"] is True and src["waiting"] == 3 and src["added_by"] == "Theo"
    note = hub.db.one("SELECT * FROM inbox WHERE title LIKE 'Theo added%'")
    assert note and note["level"] == "fyi" and note["actions"] == []
    _drain(yt)
    over = theo.client.get("/v1/youtube", headers=theo.h).json()
    assert [v["status"] for v in over["videos"]] == ["ready"] * 3 and "storage" not in over
    assert over["videos"][0]["id"].startswith("yt-") and over["videos"][0]["kind"] == "youtube"
    assert "storage" in dylan.client.get("/v1/youtube", headers=dylan.h).json()
    # Adding the same channel again doesn't duplicate it.
    theo.client.post("/v1/youtube", json={"url": "youtube.com/@MarkRober"}, headers=theo.h)
    assert len(hub.db.all("SELECT * FROM yt_sources")) == 1


def test_followed_channel_brings_new_videos(yt, people, hub):
    theo = people["phones"]["Theo"]
    theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober"}, headers=theo.h)
    _drain(yt)
    yt.feed["videos"] = [_entry(9), *yt.feed["videos"][:2]]
    assert asyncio.run(yt.refresh(force=True)) == 1
    hub.db.execute("UPDATE yt_sources SET follow=0")
    yt.feed["videos"] = [_entry(10)]
    assert asyncio.run(yt.refresh(force=True)) == 0


def test_youtube_video_downloads_like_a_film(yt, people):
    theo = people["phones"]["Theo"]
    theo.client.post("/v1/youtube", json={"url": "https://youtu.be/vid00000000"}, headers=theo.h)
    _drain(yt)
    r = theo.client.get("/v1/media/yt-vid00000000/download", headers=theo.h)
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts[0]["quality"] == "original" and opts[0]["instant"] is True and opts[0]["bytes"] == 1000
    job = theo.client.post("/v1/media/yt-vid00000000/download", json={"quality": "original"}, headers=theo.h).json()
    assert job["status"] == "ready" and job["item"]["poster_url"] == "/v1/youtube/picture/vid00000000"
    f = theo.client.get(job["url"])
    assert f.status_code == 200 and f.content == b"x" * 1000
    # The hub's own copy survives the downloads clean-up.
    yt.hub.downloads.cleanup(now=10**12)
    assert (yt.root / "vid00000000.mp4").exists()
    assert theo.client.get("/v1/youtube/picture/vid00000000", headers=theo.h).content == b"jpg"
    assert theo.client.post("/v1/media/yt-vid00000000/progress", json={"offset_s": 600, "watched": True},
                            headers=theo.h).json() == {"ok": True}
    assert yt.video("vid00000000")["watched_at"]
    assert theo.client.get("/v1/media/yt-nothere0000/download", headers=theo.h).status_code == 404


def test_parents_remove_and_block_children_cannot(yt, people, hub):
    theo, dylan = people["phones"]["Theo"], people["phones"]["Dylan"]
    src = theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober"}, headers=theo.h).json()
    _drain(yt)
    assert theo.client.delete("/v1/youtube/videos/vid00000001", headers=theo.h).status_code == 403
    assert dylan.client.delete("/v1/youtube/videos/vid00000001", headers=dylan.h).status_code == 200
    assert not (yt.root / "vid00000001.mp4").exists()
    # A removed video doesn't come back with the next check.
    asyncio.run(yt.refresh(force=True))
    assert yt.video("vid00000001")["status"] == "removed"
    assert theo.client.post("/v1/youtube/block", json={"channel_id": "UCmark"}, headers=theo.h).status_code == 403
    assert dylan.client.post("/v1/youtube/block", json={"channel_id": "UCmark", "name": "Mark Rober"},
                             headers=dylan.h).status_code == 200
    assert hub.db.all("SELECT * FROM yt_sources") == [] and hub.db.all("SELECT * FROM yt_videos") == []
    r = theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober"}, headers=theo.h)
    assert r.status_code == 400 and "turned that channel off" in r.json()["message"]
    assert src["id"]


def test_child_can_take_back_own_source_only(yt, people, hub):
    theo, avery = people["phones"]["Theo"], people["phones"]["Avery"]
    mine = avery.client.post("/v1/youtube", json={"url": "https://youtu.be/vid00000000"}, headers=avery.h).json()
    assert theo.client.delete(f"/v1/youtube/sources/{mine['id']}", headers=theo.h).status_code == 403
    his = theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober"}, headers=theo.h).json()
    assert theo.client.post(f"/v1/youtube/sources/{his['id']}", json={"follow": False}, headers=theo.h).json()["follow"] is False
    assert theo.client.delete(f"/v1/youtube/sources/{his['id']}", headers=theo.h).status_code == 200
    # vid00000000 is still Avery's; the others went with Theo's channel.
    assert [v["id"] for v in hub.db.all("SELECT id FROM yt_videos")] == ["vid00000000"]


def test_trim_removes_oldest_watched_first(yt, people, hub):
    theo = people["phones"]["Theo"]
    theo.client.post("/v1/youtube", json={"url": "https://www.youtube.com/@MarkRober"}, headers=theo.h)
    _drain(yt)
    yt.cfg.cap_gb = 2000 / 1e9
    hub.db.update("yt_videos", "id", "vid00000002", {"watched_at": 5})
    assert yt.trim() == 1
    assert yt.video("vid00000002")["status"] == "trimmed"
