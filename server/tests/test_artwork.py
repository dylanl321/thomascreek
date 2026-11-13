"""Channel logos and title art: which source wins, and the cached image's ETag."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from hub.artwork import pick_iptv_logo, pick_tmdb, pick_tvlogo, programme_key

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_matching_prefers_iptv_org_then_tvlogos_then_the_year():
    assert pick_iptv_logo([
        {"url": "https://cdn.example/old.svg", "format": "SVG", "width": 2000, "in_use": False},
        {"url": "https://cdn.example/cnn.png", "format": "PNG", "width": 512, "in_use": True},
    ]) == "https://cdn.example/cnn.png"
    logos = {
        "cnn-us": ["https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/cnn-us.png"],
        "bbc-one-uk": ["https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-kingdom/bbc-one-uk.png"],
    }
    assert pick_tvlogo(logos, ["cnn-us"], "US").endswith("/cnn-us.png")
    assert pick_tvlogo(logos, ["bbc-one", "bbc-one-uk"], "UK").endswith("/bbc-one-uk.png")
    rows = [
        {"id": 1, "title": "The Matrix Resurrections", "release_date": "2021-12-22", "poster_path": "/new.jpg"},
        {"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/old.jpg"},
    ]
    assert pick_tmdb(rows, 1999, "release_date")["id"] == 603
    assert pick_tmdb(rows, None, "release_date")["id"] == 1
    assert programme_key("Newsroom") == programme_key("  newsroom ")
    assert programme_key("Newsroom") != programme_key("Tonight")


def test_logo_source_order_and_cached_etag(hub, people):
    art = hub.artwork
    art.use_indexes(
        channels=[
            {"id": "CNN.us", "name": "CNN", "country": "US"},
            {"id": "BBCOne.uk", "name": "BBC One", "country": "UK"},
        ],
        logos=[{"channel": "CNN.us", "url": "https://logos.example/cnn.png", "format": "PNG", "width": 400, "in_use": True}],
        tvlogos={"bbc-one-uk": ["https://logos.example/bbc-one-uk.png"]},
    )
    start = datetime.fromtimestamp(time.time() - 1800, timezone.utc)
    end = start + timedelta(hours=1)
    xml = (
        f'<tv><programme start="{start.strftime("%Y%m%d%H%M%S +0000")}" '
        f'stop="{end.strftime("%Y%m%d%H%M%S +0000")}" channel="CNN.us">'
        f"<title>Newsroom</title></programme></tv>"
    ).encode()
    hub.tv_catalog.ingest(live=[
        {"stream_id": 10, "name": "CNN", "num": 20, "epg_channel_id": "CNN.us",
         "stream_icon": "https://provider.example/cnn.png"},
        {"stream_id": 11, "name": "BBC One", "num": 1, "epg_channel_id": "BBCOne.uk"},
        {"stream_id": 12, "name": "Local", "num": 2, "stream_icon": "https://provider.example/local.png"},
    ], xmltv=xml)
    assert art._channel_logo_url("xc:thomas-creek:10") == "https://logos.example/cnn.png"
    assert art._channel_logo_url("xc:thomas-creek:11") == "https://logos.example/bbc-one-uk.png"
    assert art._channel_logo_url("xc:thomas-creek:12") == "https://provider.example/local.png"

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(urlsplit_host(request))
        if request.url.host == "logos.example":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        if "api.themoviedb.org" in str(request.url):
            return httpx.Response(200, json={"results": [
                {"id": 1, "title": "The Matrix Resurrections", "release_date": "2021-12-22", "poster_path": "/new.jpg"},
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/m.jpg",
                 "backdrop_path": "/b.jpg"},
            ]})
        if request.url.host == "image.tmdb.org":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    art._transport = httpx.MockTransport(handler)
    art.remember(programme_key("The Matrix", 1999), title="The Matrix", year=1999, kind="movie")
    dylan = people["phones"]["Dylan"]
    assert dylan.client.get("/v1/art/poster/" + programme_key("The Matrix", 1999), headers=dylan.h).status_code == 404
    hub.vault.set("tmdb.api_key", "tmdb-test")

    logo = dylan.client.get("/v1/art/logo/xc:thomas-creek:10", headers=dylan.h)
    assert logo.status_code == 200, logo.text
    assert logo.content == PNG
    assert logo.headers["etag"].startswith('"')
    assert "max-age=604800" in logo.headers["cache-control"]
    again = dylan.client.get("/v1/art/logo/xc:thomas-creek:10", headers={**dylan.h, "If-None-Match": logo.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert calls.count("logos.example") == 1

    poster = dylan.client.get("/v1/art/poster/" + programme_key("The Matrix", 1999), headers=dylan.h, params={"size": "m"})
    assert poster.status_code == 200 and poster.content == PNG
    assert any("w342/m.jpg" in call for call in calls)
    assert dylan.client.get("/v1/art/poster/prog:0123456789abcdef0123", headers=dylan.h).status_code == 404
    assert people["phones"]["Dylan"].client.get("/v1/art/logo/not a key", headers=dylan.h).status_code == 404

    lineup = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    cnn = next(row for row in lineup["channels"] if row["name"] == "CNN")
    assert cnn["logo_url"] == "/v1/art/logo/xc:thomas-creek:10"
    guide = dylan.client.get("/v1/tv/guide", headers=dylan.h).json()
    now = next(row for row in guide["channels"] if row["name"] == "CNN")["now"]
    assert now["title"] == "Newsroom"
    assert now["poster_url"] == "/v1/art/poster/" + programme_key("Newsroom")
    assert now["backdrop_url"] == "/v1/art/backdrop/" + programme_key("Newsroom")


def urlsplit_host(request: httpx.Request) -> str:
    if request.url.host == "image.tmdb.org":
        return str(request.url)
    return request.url.host or ""
