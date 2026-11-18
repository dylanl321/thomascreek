"""Watching TV-library titles on the phone: the hub proxies, the provider login never reaches the phone."""

from __future__ import annotations

import httpx
import respx

from hub.routes.tvphone import LIVE, rewrite
from tests.test_tv_api import PASSWORD, _library

SERVER = "http://iptv.example:8080"


def _login(hub):
    hub.vault.set("iptv.xtream_server", SERVER)
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", PASSWORD)



def test_movie_plays_through_the_hub_with_ranges(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{SERVER}/movie/household/{PASSWORD}/101.mp4").mock(
            return_value=httpx.Response(206, content=b"abc", headers={
                "content-range": "bytes 0-2/10", "content-length": "3", "content-type": "video/mp4"}))
        play = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:101"})
        assert play.status_code == 200, play.text
        assert PASSWORD not in play.text and "iptv.example" not in play.text
        url = play.json()["url"]
        assert url.startswith("/v1/tv/phone/file/")
        got = dylan.client.get(url, headers={"Range": "bytes=0-2"})
        assert got.status_code == 206 and got.content == b"abc"
        assert got.headers["content-range"] == "bytes 0-2/10"
        assert route.calls.last.request.headers["range"] == "bytes=0-2"
        assert dylan.client.get("/v1/tv/phone/file/not-a-pass").status_code == 401


def test_kids_only_get_kids_titles(hub, people):
    _library(hub)
    _login(hub)
    theo = people["phones"]["Theo"]
    no = theo.client.post("/v1/tv/phone/play", headers=theo.h, json={"movie_id": "vod:thomas-creek:100"})
    assert no.status_code == 404
    assert theo.client.post("/v1/tv/phone/play", headers=theo.h, json={"channel_key": "xc:thomas-creek:10"}).status_code == 404
    assert theo.client.post("/v1/tv/phone/play", headers=theo.h, json={"movie_id": "vod:thomas-creek:101"}).status_code == 200
    assert theo.client.post("/v1/tv/phone/play", headers=theo.h, json={}).status_code == 400


def test_mkv_is_tv_only(hub, people, monkeypatch):
    _library(hub)
    _login(hub)
    real = hub.tv_catalog.movie
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda key: {**real(key), "ext": "mkv"})
    dylan = people["phones"]["Dylan"]
    no = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:101"})
    assert no.status_code == 409 and no.json()["error"] == "tv_only"


def test_android_phone_plays_mkv(hub, people, monkeypatch):
    _library(hub)
    _login(hub)
    real = hub.tv_catalog.movie
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda key: {**real(key), "ext": "mkv"})
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{SERVER}/movie/household/{PASSWORD}/101.mkv").mock(
            return_value=httpx.Response(200, content=b"mkv", headers={"content-length": "3"}))
        play = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                                 json={"movie_id": "vod:thomas-creek:101", "player": "exo"})
        assert play.status_code == 200, play.text
        assert PASSWORD not in play.text and "iptv.example" not in play.text
        got = dylan.client.get(play.json()["url"])
        assert got.status_code == 200 and got.content == b"mkv" and route.called
    # A file type no player opens is still refused, and the iPhone's rule is unchanged.
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda key: {**real(key), "ext": "exe"})
    odd = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                            json={"movie_id": "vod:thomas-creek:101", "player": "exo"})
    assert odd.status_code == 409 and odd.json()["error"] == "tv_only"


def test_episode_needs_the_series_opened_first(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    ok = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"episode_id": "ep:thomas-creek:900"})
    assert ok.status_code == 200 and ok.json()["kind"] == "episode"
    assert dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"episode_id": "ep:thomas-creek:999"}).status_code == 404


def test_live_playlist_is_rewritten_and_hides_the_provider(hub, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    master = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg1.ts?token=abc\n#EXTINF:6,\nhttp://edge.example/hls/seg2.ts\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/live/household/{PASSWORD}/10.m3u8").mock(
            return_value=httpx.Response(302, headers={"location": "http://edge.example/hls/abc/index.m3u8"}))
        edge = mock.get("http://edge.example/hls/abc/index.m3u8").mock(
            return_value=httpx.Response(200, text=master))
        seg = mock.get("http://edge.example/hls/abc/seg1.ts?token=abc").mock(
            return_value=httpx.Response(200, content=b"TS", headers={"content-type": "video/mp2t"}))
        play = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"})
        assert play.status_code == 200, play.text
        url = play.json()["url"]
        assert url.endswith("/index.m3u8")
        listing = dylan.client.get(url)
        assert listing.status_code == 200, listing.text
        assert "edge.example" not in listing.text and PASSWORD not in listing.text and "token=abc" not in listing.text
        lines = [line for line in listing.text.splitlines() if line and not line.startswith("#")]
        assert len(lines) == 2 and all(line.startswith("/v1/tv/phone/live/") for line in lines)
        got = dylan.client.get(lines[0])
        assert got.status_code == 200 and got.content == b"TS" and seg.called
        # A reload reads the redirected playlist, not the login address, and keeps the numbers
        again = dylan.client.get(url)
        assert [l for l in again.text.splitlines() if l and not l.startswith("#")] == lines
        assert edge.call_count == 2
        assert dylan.client.get(url.replace("index.m3u8", "s/9999")).status_code == 404


def test_rewrite_handles_variants_and_keys():
    row = LIVE.get("unit")
    text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow/index.m3u8\n'
    out = rewrite(text, "http://edge.example/a/master.m3u8", row, "TOK")
    assert 'URI="/v1/tv/phone/live/TOK/s/' in out
    assert "/v1/tv/phone/live/TOK/p/" in out and "edge.example" not in out


def test_rewrite_never_points_the_hub_at_the_house():
    row = LIVE.get("unit2")
    text = "#EXTM3U\n#EXTINF:6,\nhttp://10.0.0.10:8443/v1/admin\n#EXTINF:6,\nhttp://localhost/x.ts\n#EXTINF:6,\nseg.ts\n"
    out = rewrite(text, "http://edge.example/a/index.m3u8", row, "TOK")
    lines = [line for line in out.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 1 and row["refs"][int(lines[0].rsplit("/", 1)[-1])] == "http://edge.example/a/seg.ts"


def test_cast_receiver_can_read_the_pass_addresses(hub, people):
    """A Chromecast is another web origin: the pass routes answer CORS, the rest of the hub does not."""
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/movie/household/{PASSWORD}/101.mp4").mock(
            return_value=httpx.Response(206, content=b"abc", headers={"content-range": "bytes 0-2/10"}))
        url = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                                json={"movie_id": "vod:thomas-creek:101"}).json()["url"]
        pre = dylan.client.options(url, headers={"Origin": "https://receiver.example",
                                                 "Access-Control-Request-Method": "GET",
                                                 "Access-Control-Request-Headers": "range"})
        assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
        assert "Range" in pre.headers["access-control-allow-headers"]
        got = dylan.client.get(url, headers={"Origin": "https://receiver.example", "Range": "bytes=0-2"})
        assert got.status_code == 206 and got.headers["access-control-allow-origin"] == "*"
        assert "Content-Range" in got.headers["access-control-expose-headers"]
    other = dylan.client.get("/v1/me", headers={**dylan.h, "Origin": "https://receiver.example"})
    assert "access-control-allow-origin" not in other.headers
