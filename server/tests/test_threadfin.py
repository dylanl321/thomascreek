"""The Threadfin feed (hub/threadfin.py): token in the query, M3U and XMLTV ids that match, a relayed MPEG-TS
stream on a login of its own from the house's pool, and the connection budget (Dylan, Nov 25 2026)."""

from __future__ import annotations

import logging
import re

import httpx
import respx
from fastapi.testclient import TestClient

from hub import threadfin
from hub.iptvplay import KV_SESSIONS, lease, note_usage
from hub.main import feed_only
from tests.conftest import AWAY_IP
from tests.test_tv_api import PASSWORD, _library
from tests.test_tv_phone import SERVER, _login


def _ready(hub):
    _library(hub)
    _login(hub)
    return threadfin.token(hub)


def test_the_feed_needs_its_token_and_the_home_network(hub, home_client, app):
    tok = _ready(hub)
    assert home_client.get("/v1/threadfin/playlist.m3u").status_code == 401
    assert home_client.get("/v1/threadfin/playlist.m3u?token=nope").status_code == 401
    assert home_client.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=all").status_code == 200
    with TestClient(app, client=AWAY_IP, base_url="https://hub.test") as away:
        assert away.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=all").status_code == 404
    with TestClient(app, client=("100.106.114.22", 5000), base_url="https://hub.test") as tailscale:
        assert tailscale.get(f"/v1/threadfin/status?token={tok}").status_code == 200
    # A new token stops the old links.
    fresh = threadfin.renew(hub)
    assert fresh != tok
    assert home_client.get(f"/v1/threadfin/status?token={tok}").status_code == 401


def test_playlist_and_guide_ids_match_and_carry_no_provider_secret(hub, home_client):
    tok = _ready(hub)
    m3u = home_client.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=all")
    assert m3u.status_code == 200 and m3u.text.startswith("#EXTM3U")
    assert PASSWORD not in m3u.text and "iptv.example" not in m3u.text
    ids = re.findall(r'tvg-id="([^"]+)"', m3u.text)
    assert set(ids) == {"xc:thomas-creek:10", "xc:thomas-creek:11"}
    assert 'group-title="News"' in m3u.text and 'tvg-chno="20"' in m3u.text
    streams = [line for line in m3u.text.splitlines() if line.startswith("https://")]
    assert f"https://hub.test/v1/threadfin/stream/xc:thomas-creek:10.ts?token={tok}" in streams
    xml = home_client.get(f"/v1/threadfin/guide.xml?token={tok}&list=all")
    assert xml.status_code == 200 and PASSWORD not in xml.text
    assert set(re.findall(r'<channel id="([^"]+)"', xml.text)) == set(ids)
    assert "<title>Newsroom</title>" in xml.text and 'channel="xc:thomas-creek:10"' in xml.text
    # Kept per lineup: the same answer again, and a 304 for Threadfin's ETag.
    again = home_client.get(f"/v1/threadfin/guide.xml?token={tok}&list=all",
                            headers={"If-None-Match": xml.headers["etag"]})
    assert again.status_code == 304
    only = home_client.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=all&group=news")
    assert re.findall(r'tvg-id="([^"]+)"', only.text) == ["xc:thomas-creek:10"]
    assert home_client.get(f"/v1/threadfin/playlist.m3u?token={tok}").status_code == 200  # the basic lineup
    assert home_client.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=nope").status_code == 400


def test_the_grown_up_section_never_reaches_the_feed(hub):
    _ready(hub)
    hub.tv_catalog.ingest(
        live=[{"stream_id": 10, "name": "CNN", "num": 20, "category_id": "1"},
              {"stream_id": 66, "name": "Night", "num": 66, "category_id": "9"}],
        live_categories=[{"category_id": "1", "category_name": "News"},
                         {"category_id": "9", "category_name": "XXX Adults"}])
    keys = [row["key"] for row in threadfin.channels(hub, "all", None)]
    assert keys == ["xc:thomas-creek:10"]


def test_basic_numbers_name_their_groups():
    assert [threadfin.basic_group(n) for n in (4, 20, 31, 52, 95, 113, 203, None)] == [
        "Local", "News", "Sports", "Entertainment", "Documentary & Lifestyle", "Kids", "Movies & Premium",
        "Entertainment"]


def test_a_stream_leases_its_own_login_and_frees_it_on_hang_up(hub, home_client):
    tok = _ready(hub)
    seen: list[list[dict]] = []

    def provider(request):
        # What the pool holds while the provider is asked (the test client may read the whole body before the
        # test looks, so the lease is checked here, at the moment the stream opens).
        seen.append([row for row in hub.db.kv_get(KV_SESSIONS) or [] if str(row["tv"]).startswith("threadfin:")])
        return httpx.Response(200, content=b"\x47" * 188 * 4, headers={"content-type": "video/mp2t"})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/live/household/{PASSWORD}/10.ts").mock(side_effect=provider)
        with home_client.stream("GET", f"/v1/threadfin/stream/xc:thomas-creek:10.ts?token={tok}") as r:
            assert r.status_code == 200 and r.headers["content-type"] == "video/mp2t"
            assert b"".join(r.iter_bytes()) == b"\x47" * 188 * 4
    assert len(seen) == 1 and len(seen[0]) == 1 and seen[0][0]["login"] == "main"
    assert not [row for row in hub.db.kv_get(KV_SESSIONS) or [] if str(row["tv"]).startswith("threadfin:")]


def test_a_busy_pool_says_so_and_counts_threadfin_in_the_budget(hub, home_client):
    tok = _ready(hub)
    assert lease(hub, "xc:thomas-creek:10", "tv-living") is not None  # a TV holds the one login
    r = home_client.get(f"/v1/threadfin/stream/xc:thomas-creek:11.ts?token={tok}")
    assert r.status_code == 503 and "in use" in r.text
    status = home_client.get(f"/v1/threadfin/status?token={tok}").json()
    assert status["total"] == 1 and status["in_use"] == 1 and status["free"] == 0
    assert status["threadfin"] == 0 and status["suggested_tuners"] == 0
    note_usage(hub, "thomas-creek", "main", {"max_connections": "3", "active_cons": "1", "status": "Active"})
    status = home_client.get(f"/v1/threadfin/status?token={tok}").json()
    assert status["total"] == 3 and status["free"] == 2 and status["suggested_tuners"] == 2
    assert lease(hub, "xc:thomas-creek:11", "threadfin:abcd") is not None  # a stream the feed holds
    status = home_client.get(f"/v1/threadfin/status?token={tok}").json()
    assert status["threadfin"] == 1 and status["free"] == 1 and status["suggested_tuners"] == 2
    assert status["providers"][0]["total"] == 3


def test_a_refused_login_is_set_aside(hub, home_client):
    tok = _ready(hub)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{SERVER}/live/household/{PASSWORD}/10.ts").mock(return_value=httpx.Response(405))
        r = home_client.get(f"/v1/threadfin/stream/xc:thomas-creek:10.ts?token={tok}")
    assert r.status_code == 503 and "didn't send" in r.text
    assert PASSWORD not in r.text and "iptv.example" not in r.text
    assert any(key.startswith("thomas-creek|main|xc:thomas-creek:10") for key in hub.db.kv_get("iptv.fails") or {})
    assert not [row for row in hub.db.kv_get(KV_SESSIONS) or [] if str(row["tv"]).startswith("threadfin:")]


def test_the_token_stays_out_of_the_access_log():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("10.0.0.5:1", "GET", "/v1/threadfin/guide.xml?token=tf_abc&list=all", "1.1", 200),
                               None)
    threadfin.RedactToken().filter(record)
    assert "tf_abc" not in record.getMessage() and "list=all" in record.getMessage()


def test_the_plain_port_serves_only_the_feed(hub, app):
    tok = _ready(hub)
    plain = feed_only(app)
    with TestClient(plain, client=("192.168.1.60", 5000), base_url="http://hub.test") as c:
        assert c.get("/v1/ping").status_code == 404
        assert c.get("/admin").status_code == 404
        r = c.get(f"/v1/threadfin/playlist.m3u?token={tok}&list=all")
        assert r.status_code == 200 and "http://hub.test/v1/threadfin/stream/" in r.text


def test_the_web_page_shows_the_links_and_an_agent_never_sees_the_token(hub, people, home_client):
    from hub import agentlogins
    from tests.test_admin_page import _sign_in

    tok = _ready(hub)
    csrf = _sign_in(people, home_client)
    page = home_client.get("/admin/watching").text
    assert "Plex Live TV feed" in page and f"/v1/threadfin/playlist.m3u?token={tok}" in page
    assert "http://hub.test:8780/v1/threadfin/guide.xml" in page
    r = home_client.post("/admin/threadfin/renew", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and threadfin.token(hub) != tok
    _, key = agentlogins.create(hub.db, "claude-mac")
    home_client.cookies.clear()
    seen = home_client.get("/admin/watching", headers={"Authorization": f"Bearer {key}"}).text
    assert "Plex Live TV feed" in seen and threadfin.token(hub) not in seen
    # The agent login reads the feed itself (to time it after a deploy), never through the token.
    assert home_client.get("/v1/threadfin/status", headers={"Authorization": f"Bearer {key}"}).status_code == 200
