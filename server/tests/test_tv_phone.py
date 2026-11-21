"""Watching TV-library titles on the phone: the hub proxies, the provider login never reaches the phone."""

from __future__ import annotations

import json

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


def test_iphone_media_app_plays_mkv(hub, people, monkeypatch):
    """`player: "ks"` (the iPhone and iPad media app) opens every file type, like the Android phone's `exo`."""
    _library(hub)
    _login(hub)
    real = hub.tv_catalog.movie
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda key: {**real(key), "ext": "mkv"})
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/movie/household/{PASSWORD}/101.mkv").mock(
            return_value=httpx.Response(200, content=b"mkv", headers={"content-length": "3"}))
        play = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                                 json={"movie_id": "vod:thomas-creek:101", "player": "ks"})
        assert play.status_code == 200, play.text
        assert PASSWORD not in play.text and "iptv.example" not in play.text
        assert dylan.client.get(play.json()["url"]).content == b"mkv"
    # A player the hub does not know keeps the MP4 rule.
    no = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                           json={"movie_id": "vod:thomas-creek:101", "player": "other"})
    assert no.status_code == 409 and no.json()["error"] == "tv_only"


def test_iphone_media_app_keeps_the_kids_rule(hub, people):
    """The player value never widens what a child may play."""
    _library(hub)
    _login(hub)
    theo = people["phones"]["Theo"]
    for ask in ({"movie_id": "vod:thomas-creek:100"}, {"channel_key": "xc:thomas-creek:10"}):
        no = theo.client.post("/v1/tv/phone/play", headers=theo.h, json={**ask, "player": "ks"})
        assert no.status_code == 404


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


def test_live_relay_says_what_the_provider_answered_and_tries_the_backup(hub, people):
    """Nov 21 2026: a channel relayed to a phone abroad failed in half a second with nothing to say why. A refusal
    now moves to the provider's backup address, the phone player's own name is sent, and a failure keeps each
    address's answer (never the address) in /v1/ping/timings."""
    from hub import iptvaddr

    backup = "http://backup.example:8080"
    _library(hub)
    _login(hub)
    iptvaddr.set_backups(hub.db, "thomas-creek", [backup], SERVER)
    dylan = people["phones"]["Dylan"]
    player = {**dylan.h, "User-Agent": "AppleCoreMedia/1.0.0 (iPhone)"}
    with respx.mock(assert_all_called=False) as mock:
        main = mock.get(f"{SERVER}/live/household/{PASSWORD}/10.m3u8").mock(
            return_value=httpx.Response(403, text="forbidden"))
        spare = mock.get(f"{backup}/live/household/{PASSWORD}/10.m3u8").mock(
            return_value=httpx.Response(200, text="#EXTM3U\n#EXTINF:6,\nseg1.ts\n"))
        url = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"}
                                ).json()["url"]
        got = dylan.client.get(url, headers=player)
        assert got.status_code == 200, got.text
        assert main.called and spare.called
        assert spare.calls.last.request.headers["user-agent"] == "AppleCoreMedia/1.0.0 (iPhone)"
        # A refusal belongs to the login, not the address: the main address isn't set aside.
        assert iptvaddr.best(hub.db, "thomas-creek", SERVER) == SERVER

        spare.mock(return_value=httpx.Response(200, text="<html>not today</html>",
                                               headers={"content-type": "text/html; charset=utf-8"}))
        url = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"}
                                ).json()["url"]
        failed = dylan.client.get(url, headers=player)
    assert failed.status_code == 502 and failed.json()["message"] == "The channel didn't answer"
    assert "up0.http403;dur=" in failed.headers["server-timing"]
    assert "up1.notplaylist.html;dur=" in failed.headers["server-timing"]
    kept = next(r for r in hub.timings.slow() if r["route"].endswith("/index.m3u8"))
    assert kept["status"] == 502 and "up0.http403" in kept["steps"] and "up1.notplaylist.html" in kept["steps"]
    assert PASSWORD not in json.dumps(kept) and "backup.example" not in json.dumps(kept)


def test_a_channel_without_hls_plays_as_one_ts_stream(hub, people, monkeypatch):
    """Nov 21 2026: the provider answered 405 to `.m3u8` for one sports channel, at home and abroad, while the TVs
    play its `.ts`. Phones get the MPEG-TS form too: the provider's own (direct) and the hub's relay."""
    _library(hub)
    _login(hub)
    monkeypatch.setattr(hub.config.hub, "remote_url", "https://hub.example/")
    dylan = people["phones"]["Dylan"]
    direct = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                               json={"channel_key": "xc:thomas-creek:10", "player": "vlc", "direct": True}).json()
    assert direct["url"].endswith("/10.m3u8")
    assert direct["ts_urls"][0] == direct["url"][:-5] + ".ts"
    assert direct["ts_urls"][-1] == "https://hub.example" + direct["relay_url"].replace("/index.m3u8", "/stream.ts")
    assert not any(u.endswith(".ts") for u in direct.get("alternates") or [])  # older players never get TS there
    relayed = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"}).json()
    assert relayed["ts_url"] == relayed["url"].replace("/index.m3u8", "/stream.ts")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/live/household/{PASSWORD}/10.m3u8").mock(return_value=httpx.Response(405))
        ts = mock.get(f"{SERVER}/live/household/{PASSWORD}/10.ts").mock(
            return_value=httpx.Response(200, content=b"G" * 188, headers={"content-type": "video/mp2t"}))
        assert dylan.client.get(relayed["url"]).status_code == 502
        got = dylan.client.get(relayed["ts_url"])
    assert got.status_code == 200 and got.content == b"G" * 188 and ts.called
    assert got.headers["content-type"] == "video/mp2t" and PASSWORD not in relayed["ts_url"]
    films = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:101"}).json()
    assert "ts_url" not in films


def test_the_hub_remembers_a_channel_without_hls_and_leads_with_its_ts(hub, people, monkeypatch):
    """After the provider answers 405 to a channel's `.m3u8`, the next play leads with its `.ts` (no failed first
    try on the phone), keeps the `.m3u8` as a fallback, and forgets it once a playlist plays again."""
    _library(hub)
    _login(hub)
    monkeypatch.setattr(hub.config.hub, "remote_url", "https://hub.example/")
    dylan = people["phones"]["Dylan"]

    def play(direct: bool) -> dict:
        body = {"channel_key": "xc:thomas-creek:10", **({"player": "vlc", "direct": True} if direct else {})}
        return dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json=body).json()

    first = play(True)
    assert first["url"].endswith(".m3u8") and "hls" not in first
    relayed = play(False)
    with respx.mock(assert_all_called=False) as mock:
        hls = mock.get(f"{SERVER}/live/household/{PASSWORD}/10.m3u8").mock(return_value=httpx.Response(405))
        assert dylan.client.get(relayed["url"]).status_code == 502
        again = play(True)
        assert again["hls"] is False and again["url"] == again["ts_urls"][0] and again["url"].endswith("/10.ts")
        assert again["alternates"][0].endswith("/10.m3u8") and again["alternates"][-1].endswith("/index.m3u8")
        relayed = play(False)
        assert relayed["hls"] is False
        hls.mock(return_value=httpx.Response(200, text="#EXTM3U\n#EXTINF:6,\nseg1.ts\n"))
        assert dylan.client.get(relayed["url"]).status_code == 200
    assert "hls" not in play(True)


def test_a_channel_the_provider_refuses_everywhere_says_so_at_once(hub, people):
    """Nov 21 2026: one sports channel answered 405 to both `.ts` and `.m3u8` on the only login, and the phone went
    through 40 s of tries. A refused `.ts` sets that copy aside; the next /play answers 503 `refused` at once."""
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    relayed = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"}).json()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/live/household/{PASSWORD}/10.ts").mock(return_value=httpx.Response(405))
        assert dylan.client.get(relayed["ts_url"]).status_code == 502
    again = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"})
    assert again.status_code == 503 and again.json()["error"] == "refused"
    direct = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                               json={"channel_key": "xc:thomas-creek:10", "player": "vlc", "direct": True})
    assert direct.status_code == 503 and direct.json()["error"] == "refused"
    # Another channel on the same login still plays.
    other = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:11"})
    assert other.status_code == 200, other.text


def test_the_catalog_is_read_once_at_start_for_plays(hub):
    import asyncio

    from hub import iptvcanon

    iptvcanon._INDEX.pop(id(hub.db), None)
    asyncio.run(hub.warm_plays())
    assert id(hub.db) in iptvcanon._INDEX


def test_an_old_pass_never_opens_a_second_stream_on_a_login_someone_else_holds(hub, people):
    """One login carries one stream. A pass from an earlier play must not open the provider on a login the pool has
    given to another player since: the provider would cut one of the two streams (Nov 21 2026)."""
    from hub import iptvplay

    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    relayed = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"channel_key": "xc:thomas-creek:10"}).json()
    iptvplay.release_tv(hub, f"phone:{dylan.device_id}")
    assert iptvplay.lease(hub, "xc:thomas-creek:11", "tv-other") is not None  # the only login goes to a TV
    with respx.mock(assert_all_called=False) as mock:
        ts = mock.get(f"{SERVER}/live/household/{PASSWORD}/10.ts").mock(return_value=httpx.Response(405))
        got = dylan.client.get(relayed["ts_url"])
    assert got.status_code == 409 and got.json()["error"] == "moved"
    assert not ts.called
    assert iptvplay.held_by(hub, "tv-other") is not None  # the TV's stream is left alone
