"""Downloads for offline viewing: options, original passthrough, rewrap, children, progress."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import httpx
import pytest
import respx

from hub.downloads import Downloads, Job, options, original_mode

PLEX = "http://plex.test"


def _meta(rating="PG", container="mp4", vcodec="h264", acodec="aac", kind="movie"):
    return {"MediaContainer": {"Metadata": [{
        "ratingKey": "42", "type": kind, "title": "Moana 2", "year": 2024, "contentRating": rating,
        "duration": 6_000_000, "thumb": "/library/metadata/42/thumb/1",
        "Media": [{"videoCodec": vcodec, "audioCodec": acodec, "container": container, "videoResolution": "1080",
                   "Part": [{"key": "/library/parts/7/123/file." + container, "size": 2_000_000_000,
                             "container": container}]}],
    }]}}


@pytest.fixture
def plex(hub, tmp_path):
    hub.config.plex.enabled = True
    hub.config.plex.url = PLEX
    hub.vault.set("plex.token", "tok")
    hub.downloads = Downloads(hub, tmp_path / "downloads")
    return hub


def test_options_and_modes():
    mp4 = {"video_codec": "h264", "audio_codec": "aac", "container": "mp4", "duration_ms": 3_600_000, "size": 9}
    assert original_mode(mp4) == "direct"
    assert original_mode({**mp4, "container": "mkv"}) == "remux"
    assert original_mode({**mp4, "video_codec": "vc1"}) is None
    assert original_mode({**mp4, "audio_codec": "dts"}) is None
    q = [o["quality"] for o in options(mp4)]
    assert q == ["original", "high", "small"]
    assert options(mp4)[0]["instant"] is True
    assert [o["quality"] for o in options({**mp4, "video_codec": "mpeg2video"})] == ["high", "small"]
    assert 1_500_000_000 < options(mp4)[1]["bytes"] < 2_200_000_000  # ~4.2 Mbps for an hour


def test_original_mp4_streams_from_plex_with_range(plex, people):
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_meta())
        part = mock.get(f"{PLEX}/library/parts/7/123/file.mp4").respond(
            206, content=b"abcd", headers={"Content-Range": "bytes 0-3/2000000000", "Content-Length": "4"})
        r = dylan.client.get("/v1/media/42/download", headers=dylan.h)
        assert r.status_code == 200, r.text
        assert r.json()["options"][0] == {"quality": "original", "label": "Original",
                                          "detail": "1080p quality, fastest to get", "bytes": 2_000_000_000,
                                          "instant": True}
        r = dylan.client.post("/v1/media/42/download", json={"quality": "original"}, headers=dylan.h)
        job = r.json()
        assert job["status"] == "ready" and job["url"].startswith("/v1/media/file/")
        assert job["item"]["title"] == "Moana 2"
        f = dylan.client.get(job["url"], headers={"Range": "bytes=0-3"})
        assert f.status_code == 206 and f.content == b"abcd"
        assert f.headers["content-range"] == "bytes 0-3/2000000000"
        sent = part.calls.last.request
        assert sent.headers["Range"] == "bytes=0-3" and sent.headers["X-Plex-Token"] == "tok"
        # The same request again reuses the job; another phone can't read its status.
        assert dylan.client.post("/v1/media/42/download", json={"quality": "original"}, headers=dylan.h).json()["id"] == job["id"]
        avery = people["phones"]["Avery"]
        assert avery.client.get(f"/v1/media/downloads/{job['id']}", headers=avery.h).status_code == 404
        assert dylan.client.get("/v1/media/file/not-a-pass").status_code == 401


def test_children_download_rated_titles_without_asking(plex, people):
    theo = people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_meta(rating="R"))
        assert theo.client.post("/v1/media/42/download", json={"quality": "original"}, headers=theo.h).status_code == 404
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_meta(rating="PG"))
        r = theo.client.post("/v1/media/42/download", json={"quality": "original"}, headers=theo.h)
        assert r.status_code == 200 and r.json()["status"] == "ready"


def test_original_not_offered_when_iphone_cant_play_it(plex, people):
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_meta(vcodec="vc1", container="mkv"))
        r = dylan.client.post("/v1/media/42/download", json={"quality": "original"}, headers=dylan.h)
        assert r.json()["status"] == "failed" and "Good or Small" in r.json()["error"]


def test_progress_goes_back_to_plex(plex, people):
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        tl = mock.get(f"{PLEX}/:/timeline").respond(200)
        sc = mock.get(f"{PLEX}/:/scrobble").respond(200)
        assert dylan.client.post("/v1/media/42/progress", json={"offset_s": 61.5, "duration_s": 6000},
                                 headers=dylan.h).json() == {"ok": True}
        p = tl.calls.last.request.url.params
        assert p["ratingKey"] == "42" and p["time"] == "61500" and p["state"] == "stopped"
        dylan.client.post("/v1/media/42/progress", json={"offset_s": 0, "watched": True}, headers=dylan.h)
        assert sc.calls.last.request.url.params["key"] == "42"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_mkv_is_rewrapped_into_mp4(plex, tmp_path):
    src = tmp_path / "in.mkv"
    made = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                           "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-c:a", "aac",
                           "-y", str(src)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("ffmpeg can't make a test file here")
    d = plex.downloads
    d._input_for = lambda job, media, mode: (str(src), [])  # type: ignore[method-assign]
    media = {"video_codec": "h264", "audio_codec": "aac", "container": "mkv", "duration_ms": 2000,
             "part_key": "/library/parts/1/1/file.mkv"}
    job = await d.start("42", "original", media, {"title": "x"}, "dev1", None)
    assert job.status == "preparing"
    await asyncio.wait_for(job.task, 30)
    assert job.status == "ready" and job.path.suffix == ".mp4" and job.bytes > 0
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0",
                            str(job.path)], capture_output=True, text=True)
    assert "mp4" in probe.stdout
    # A day later the file is cleaned up.
    assert d.cleanup(now=job.created_at + 25 * 3600) == 1
    assert not job.path.exists()


def test_cleanup_keeps_fresh_jobs(tmp_path, hub):
    d = Downloads(hub, tmp_path)
    d.jobs["a"] = Job(id="a", item_id="1", quality="original", device_id="x", person_id=None, item={},
                      status="ready")
    assert d.cleanup() == 0 and "a" in d.jobs
    assert httpx  # imported for respx


def test_show_episodes_inherit_rating(plex, people):
    theo = people["phones"]["Theo"]
    show = {"MediaContainer": {"Metadata": [{"ratingKey": "9", "type": "show", "title": "Bluey", "contentRating": "TV-Y"}]}}
    eps = {"MediaContainer": {"Metadata": [
        {"ratingKey": "10", "type": "episode", "title": "Keepy Uppy", "grandparentTitle": "Bluey", "parentIndex": 1, "index": 4},
        {"ratingKey": "11", "type": "episode", "title": "Late one", "grandparentTitle": "Bluey", "contentRating": "TV-14"}]}}
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/9").respond(json=show)
        mock.get(f"{PLEX}/library/metadata/9/allLeaves").respond(json=eps)
        r = theo.client.get("/v1/media/9/episodes", headers=theo.h)
        assert [i["id"] for i in r.json()["items"]] == ["10"]
        assert r.json()["items"][0]["title"] == "Bluey: Keepy Uppy"
