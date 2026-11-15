from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tests.test_home_alerts import _door
from tests.test_tv_api import KID_TV, TV_ID, Clock, Key, _auth, _enroll, _token


def _tv(hub, client, install_id, **kw):
    key = Key()
    _enroll(client, key, install_id, "TV")
    hub.tv_app.set_tv(install_id, status="approved", **kw)
    return _token(client, key, Clock(), install_id)["access_token"]


def test_the_cameras_screen_lists_and_plays_through_the_hub(hub, home_client, people):
    _door(hub)
    token = _tv(hub, home_client, TV_ID, room="living_room")
    page = home_client.get("/v1/tv/cameras", headers=_auth(token))
    assert page.status_code == 200, page.text
    rows = page.json()["cameras"]
    ids = [row["id"] for row in rows]
    assert "kitchen" in ids and "driveway" in ids
    assert rows[0]["door"] is True  # door cameras first
    kitchen = next(row for row in rows if row["id"] == "kitchen")
    assert kitchen["snapshot_url"] == "/v1/tv/cameras/kitchen/snapshot.jpg"
    assert "10.0." not in page.text and "rtsp" not in page.text

    async def jpg(_cam, height=360):
        return b"\xff\xd8frame"

    async def proxy(_entity):
        return b"\xff\xd8frame"

    hub.frigate.latest_jpg = jpg
    hub.ha.camera_proxy = proxy
    shot = home_client.get(kitchen["snapshot_url"], headers=_auth(token))
    assert shot.status_code == 200 and shot.content == b"\xff\xd8frame"
    assert home_client.get("/v1/tv/cameras/nope/snapshot.jpg", headers=_auth(token)).status_code == 404

    hub.frigate.cfg.go2rtc_url = "http://go2rtc.test:1984"

    async def playlist(_cam):
        return "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhls/playlist.m3u8?id=abc\n"

    hub.frigate.hls_playlist = playlist
    live = home_client.get(kitchen["stream_url"], headers=_auth(token))
    assert live.status_code == 200 and "mpegurl" in live.headers["content-type"]
    line = [ln for ln in live.text.splitlines() if ln.startswith("/v1/")][0]
    assert line.startswith("/v1/tv/cameras/kitchen/media?")
    assert parse_qs(urlparse(line).query)["src"][0] == "api/hls/playlist.m3u8?id=abc"
    assert "go2rtc.test" not in live.text


def test_kids_and_phones_do_not_get_the_cameras_screen(hub, home_client, people):
    _door(hub)
    kid = _tv(hub, home_client, KID_TV, profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    assert home_client.get("/v1/tv/cameras", headers=_auth(kid)).status_code == 403
    assert home_client.get("/v1/tv/cameras/kitchen/snapshot.jpg", headers=_auth(kid)).status_code == 403
    dylan = people["phones"]["Dylan"]
    assert home_client.get("/v1/tv/cameras", headers=dylan.h).status_code == 403
