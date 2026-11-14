"""Up Next is ranked from what a profile has actually watched."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hub.upnext import remember, shelf

from tests.test_tv_api import _xt


def _catalog(hub):
    hub.tv_app.set_kids_categories(["Kids"])
    now = datetime.now(timezone.utc)
    xml = f"""<?xml version="1.0"?><tv>
      <programme start="{_xt(now - timedelta(minutes=2))}" stop="{_xt(now + timedelta(minutes=40))}" channel="cnn.us">
        <title>Newsroom</title>
      </programme>
      <programme start="{_xt(now - timedelta(minutes=2))}" stop="{_xt(now + timedelta(minutes=20))}" channel="toon.us">
        <title>Blue</title>
      </programme>
    </tv>""".encode()
    hub.tv_catalog.ingest(
        live=[
            {"stream_id": 10, "name": "CNN", "category_id": "1", "epg_channel_id": "cnn.us"},
            {"stream_id": 11, "name": "Cartoon", "category_id": "2", "epg_channel_id": "toon.us"},
            {"stream_id": 12, "name": "Sports", "category_id": "1"},
        ],
        live_categories=[{"category_id": "1", "category_name": "News"}, {"category_id": "2", "category_name": "Kids"}],
        vod=[{"stream_id": 101, "name": "Kids Movie", "category_id": "4"},
             {"stream_id": 100, "name": "Drama Movie", "category_id": "3"}],
        vod_categories=[{"category_id": "3", "category_name": "Drama"}, {"category_id": "4", "category_name": "Kids"}],
        series=[{"series_id": 50, "name": "Cartoon Show", "category_id": "5"}],
        series_categories=[{"category_id": "5", "category_name": "Kids"}],
        xmltv=xml,
    )
    hub.tv_catalog.store_series({
        "info": {"name": "Cartoon Show"},
        "episodes": {"1": [
            {"id": "900", "episode_num": 1, "title": "Pilot", "info": {"duration_secs": 1200}},
            {"id": "901", "episode_num": 2, "title": "Next", "info": {"duration_secs": 1200}},
        ]},
    }, "series:thomas-creek:50")


def test_shelf_ranks_history_and_hides_what_a_child_cannot_watch(hub, people):
    _catalog(hub)
    assert shelf(hub, "adult") == {"rows": []}
    now = datetime.now(timezone.utc).timestamp()
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}, now=now - 4000)
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:10", "title": "Newsroom"}, now=now)
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "position_s": 600}, now=now)
    remember(hub.db, "adult", {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "position_s": 50}, now=now)
    remember(hub.db, "adult", {
        "series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:900",
        "title": "Pilot", "position_s": 80,
    }, now=now)
    remember(hub.db, "adult", {"channel_key": "xc:thomas-creek:12", "title": "Sports"}, now=now)
    remember(hub.db, "kids", {"channel_key": "xc:thomas-creek:11", "title": "Blue"}, now=now)
    hub.db.kv_set("dvr.recordings", [{
        "id": "dvr_abc123", "title": "Newsroom", "status": "done", "kids": False,
        "channel_key": "xc:thomas-creek:10", "poster_url": "/v1/art/poster/prog:abc",
        "capture_end": now,
    }, {
        "id": "dvr_kids1", "title": "Blue", "status": "done", "kids": True,
        "channel_key": "xc:thomas-creek:11", "capture_end": now - 10,
    }, {
        "id": "dvr_later", "title": "Later", "status": "scheduled", "kids": False, "capture_end": now,
    }])

    adult = shelf(hub, "adult")
    by_id = {row["id"]: row for row in adult["rows"]}
    assert list(by_id) == ["continue", "next", "recordings", "onnow", "trending"]
    continued = {item.get("movie_id") or item.get("episode_id") for item in by_id["continue"]["items"]}
    assert "vod:thomas-creek:101" in continued and "ep:thomas-creek:900" in continued
    assert by_id["continue"]["items"][0]["reason"] == "continue watching"
    assert by_id["next"]["items"][0]["episode_id"] == "ep:thomas-creek:901"
    assert by_id["next"]["items"][0]["reason"] == "new episode of a series you watch or record"
    assert [item["recording_id"] for item in by_id["recordings"]["items"]] == ["dvr_abc123", "dvr_kids1"]
    assert by_id["onnow"]["items"][0]["title"] == "Newsroom"
    assert by_id["onnow"]["items"][0]["channel_key"] == "xc:thomas-creek:10"
    assert by_id["onnow"]["items"][0]["reason"] == "starting now on a channel you watch"
    assert [item["channel_key"] for item in by_id["trending"]["items"]] == ["xc:thomas-creek:12"]
    assert by_id["trending"]["items"][0]["reason"] == "trending in your history"
    assert "://" not in str(adult)

    kids = shelf(hub, "kids")
    blob = str(kids)
    assert "Drama" not in blob and "Newsroom" not in blob and "dvr_abc123" not in blob
    assert any(item.get("channel_key") == "xc:thomas-creek:11" for row in kids["rows"] for item in row["items"])

    dylan = people["phones"]["Dylan"]
    theo = people["phones"]["Theo"]
    grown = dylan.client.get("/v1/tv/upnext", headers=dylan.h)
    assert grown.status_code == 200, grown.text
    assert grown.json()["rows"][0]["id"] == "continue"
    child = theo.client.get("/v1/tv/upnext", headers=theo.h, params={"profile": "adult"})
    assert child.status_code == 200, child.text
    assert "Drama" not in child.text and "Newsroom" not in child.text
    explicit = dylan.client.get("/v1/tv/upnext", headers=dylan.h, params={"profile": "kids"})
    assert "dvr_abc123" not in explicit.text
