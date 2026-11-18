"""Casting from the phone: the receiver's content type, a converted copy for files a Cast TV can't open, and the
phone's report of what the TV did, so a black screen can be traced."""

from __future__ import annotations

import json
import stat

from hub.routes import tvphone
from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import _login

MOVIE = "vod:thomas-creek:101"


def _mkv(hub, monkeypatch):
    real = hub.tv_catalog.movie
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda key: {**real(key), "ext": "mkv"})


def test_an_mp4_casts_as_it_is_with_the_converted_copy_behind_it(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    body = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": MOVIE, "cast": True}).json()
    assert body["url"].startswith("/v1/tv/phone/file/") and body["content_type"] == "video/mp4"
    assert body["fallback_url"].startswith("/v1/tv/phone/cast/") and body["fallback_content_type"] == "video/mp4"
    assert "converted" not in body and "cast_note" not in body
    live = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"channel_key": "xc:thomas-creek:10", "cast": True}).json()
    assert live["content_type"] == "application/x-mpegURL" and live["fallback_url"].startswith("/v1/tv/phone/cast/")


def test_an_mkv_casts_as_the_converted_copy(hub, people, monkeypatch):
    _library(hub)
    _login(hub)
    _mkv(hub, monkeypatch)
    dylan = people["phones"]["Dylan"]
    cast = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": MOVIE, "player": "exo", "cast": True}).json()
    assert cast["url"].startswith("/v1/tv/phone/cast/") and cast["converted"] is True
    assert cast["content_type"] == "video/mp4" and "MKV" in cast["cast_note"]
    assert cast["fallback_url"].startswith("/v1/tv/phone/file/")
    assert cast["fallback_content_type"] == "video/x-matroska"
    # Not casting (an older phone, or its own player): the file as before, the copy offered beside it.
    own = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": MOVIE, "player": "exo"}).json()
    assert own["url"].startswith("/v1/tv/phone/file/") and own["cast_url"].startswith("/v1/tv/phone/cast/")
    assert PASSWORD not in json.dumps(cast) + json.dumps(own)


def test_convert_keeps_h264_and_makes_the_rest_playable():
    keep = tvphone.convert_args("ffmpeg", "http://p/x.mkv", "h264", 600, live=False)
    assert keep[keep.index("-c:v") + 1] == "copy" and keep[keep.index("-ss") + 1] == "600"
    assert keep[keep.index("-c:a") + 1] == "aac" and "frag_keyframe+empty_moov+default_base_moof" in keep
    hevc = tvphone.convert_args("ffmpeg", "http://p/x.mkv", "hevc", 0, live=False)
    assert hevc[hevc.index("-c:v") + 1] == "libx264" and "-ss" not in hevc
    live = tvphone.convert_args("ffmpeg", "http://p/x.m3u8", None, 600, live=True)
    assert "-ss" not in live and live[live.index("-c:v") + 1] == "libx264"


def test_the_converted_copy_streams_and_answers_a_cast_receiver(hub, people, monkeypatch, tmp_path):
    _library(hub)
    _login(hub)
    _mkv(hub, monkeypatch)
    seen = tmp_path / "args"
    fake = tmp_path / "ffmpeg"
    fake.write_text(f'#!/bin/sh\necho "$@" > {seen}\nprintf FMP4DATA\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(tvphone.shutil, "which", lambda name: str(fake) if name == "ffmpeg" else None)

    async def codec(url):
        return "h264"

    monkeypatch.setattr(tvphone, "video_codec", codec)
    dylan = people["phones"]["Dylan"]
    url = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                            json={"movie_id": MOVIE, "player": "exo", "cast": True}).json()["url"]
    head = dylan.client.head(url)
    assert head.status_code == 200 and head.headers["content-type"] == "video/mp4" and not seen.exists()
    got = dylan.client.get(url + "?t=90", headers={"Origin": "https://receiver.example"})
    assert got.status_code == 200 and got.content == b"FMP4DATA"
    assert got.headers["content-type"] == "video/mp4" and got.headers["access-control-allow-origin"] == "*"
    args = seen.read_text()
    assert "-ss 90" in args and "-c:v copy" in args and "101.mkv" in args
    logged = hub.db.all("SELECT params FROM activity WHERE action='tv.phone.cast'")
    said = json.dumps([row["params"] for row in logged])
    assert logged and PASSWORD not in said and "iptv.example" not in said
    assert dylan.client.get("/v1/tv/phone/cast/not-a-pass").status_code == 401


def test_the_phone_reports_what_the_tv_did(hub, people):
    dylan = people["phones"]["Dylan"]
    r = dylan.client.post("/v1/tv/phone/cast/report", headers=dylan.h, json={
        "kind": "movie", "id": MOVIE, "state": "failed", "code": 104, "reason": "This TV can't play this file",
        "attempt": "original", "receiver": "Living room"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = hub.db.all("SELECT outcome, target, params FROM activity WHERE action='tv.phone.cast'")[-1]
    assert row["outcome"] == "error" and row["target"] == MOVIE and row["params"]["code"] == 104
    assert dylan.client.post("/v1/tv/phone/cast/report", json={"state": "playing"}).status_code == 401
