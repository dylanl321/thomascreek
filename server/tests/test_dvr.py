"""Scheduling a recording, refusing a second stream, keeping it across a restart, and Range playback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hub.routes.home import shape_event


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _channel(hub) -> None:
    hub.vault.set("iptv.xtream_server", "http://iptv.example:8080")
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", "s3cret")
    hub.tv_catalog.ingest(live=[
        {"stream_id": 10, "name": "CNN", "num": 20, "epg_channel_id": "CNN.us", "category_id": "1"},
        {"stream_id": 11, "name": "Cartoon", "num": 3, "category_id": "2"},
    ], live_categories=[{"category_id": "2", "category_name": "Kids"}])
    hub.tv_app.set_kids_categories(["Kids"])


def _grown(hub, people):
    phone = people["phones"]["Dylan"]
    return hub.subject_for(hub.db.device(phone.device_id), True)


def test_schedule_conflict_and_record_now(hub, people):
    _channel(hub)
    hub.dvr.max_connections = 1
    dylan = people["phones"]["Dylan"]
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    end = start + timedelta(hours=1)
    body = {"channel_key": "xc:thomas-creek:10", "start": _iso(start), "end": _iso(end), "title": "Newsroom"}
    first = dylan.client.post("/v1/dvr/recordings", headers=dylan.h, json=body)
    assert first.status_code == 201, first.text
    rec = first.json()
    assert rec["status"] == "scheduled" and rec["title"] == "Newsroom"
    assert rec["channel_key"] == "xc:thomas-creek:10"
    assert rec["poster_url"].startswith("/v1/art/poster/prog:")
    assert rec["stream_url"] == f"/v1/dvr/recordings/{rec['id']}/stream"
    assert rec["size_bytes"] == 0 and rec["duration_s"] == 0 and rec["error"] is None
    assert "s3cret" not in first.text
    overlap = dylan.client.post("/v1/dvr/recordings", headers=dylan.h, json={
        **body, "start": _iso(start + timedelta(minutes=10)), "end": _iso(end + timedelta(minutes=10)),
    })
    assert overlap.status_code == 409 and overlap.json()["error"] == "conflict"
    later = dylan.client.post("/v1/dvr/recordings", headers=dylan.h, json={
        "channel_key": "iptv-org:CNN.us", "start": _iso(end + timedelta(hours=1)),
        "end": _iso(end + timedelta(hours=2)), "title": "Later",
    })
    assert later.status_code == 201, later.text
    assert later.json()["channel_key"] == "iptv-org:CNN.us"
    listed = dylan.client.get("/v1/dvr/recordings", headers=dylan.h, params={"status": "scheduled"}).json()
    assert {row["title"] for row in listed["recordings"]} == {"Newsroom", "Later"}
    kid = people["phones"]["Theo"].client.post("/v1/dvr/recordings", headers=people["phones"]["Theo"].h, json=body)
    assert kid.status_code == 403
    live = dylan.client.post("/v1/dvr/recordings/now", headers=dylan.h, json={
        "channel_key": "xc:thomas-creek:11", "duration_s": 120, "title": "Cartoon",
    })
    assert live.status_code == 201, live.text
    assert live.json()["status"] == "recording"
    assert "s3cret" not in live.text
    event = {"type": "dvr_update", "data": {**rec, "kids": False}}
    shaped = shape_event(hub, _grown(hub, people), event)
    assert shaped == {"type": "dvr_update", "data": rec}


def test_restart_keeps_the_schedule(hub, people):
    _channel(hub)
    dylan = people["phones"]["Dylan"]
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    end = start + timedelta(minutes=30)
    made = dylan.client.post("/v1/dvr/recordings", headers=dylan.h, json={
        "channel_key": "xc:thomas-creek:10", "start": _iso(start), "end": _iso(end), "title": "Newsroom",
    }).json()
    fresh = type(hub.dvr)(hub, hub.dvr.dir)
    assert fresh.get(made["id"])["status"] == "scheduled"
    fresh.now = lambda: end.timestamp() + 3600
    fresh.recover()
    missed = fresh.get(made["id"])
    assert missed["status"] == "failed" and "down" in (missed["error"] or "")
    tv = _grown(hub, people)
    tv.tv = True
    tv.tv_id = "tv_office_0123456789"
    event = {"type": "dvr_update", "data": {**fresh.public(missed), "kids": False}}
    assert shape_event(hub, tv, event)["data"]["status"] == "failed"
    assert "kids" not in shape_event(hub, tv, event)["data"]


def test_range_plays_a_growing_file(hub, people):
    _channel(hub)
    dylan = people["phones"]["Dylan"]
    start = datetime.now(timezone.utc) + timedelta(hours=5)
    end = start + timedelta(minutes=20)
    made = dylan.client.post("/v1/dvr/recordings", headers=dylan.h, json={
        "channel_key": "xc:thomas-creek:10", "start": _iso(start), "end": _iso(end), "title": "Newsroom",
    }).json()
    path = hub.dvr._file(made["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a" * 1000)
    part = dylan.client.get(made["stream_url"], headers={**dylan.h, "Range": "bytes=10-19"})
    assert part.status_code == 206, part.text
    assert part.content == b"a" * 10
    assert part.headers["content-range"] == "bytes 10-19/1000"
    assert part.headers["content-type"].startswith("video/mp2t")
    whole = dylan.client.get(made["stream_url"], headers=dylan.h)
    assert whole.status_code == 200 and len(whole.content) == 1000
    assert dylan.client.post(f"/v1/dvr/recordings/{made['id']}/stop", headers=dylan.h).json()["status"] == "cancelled"
    assert dylan.client.delete(f"/v1/dvr/recordings/{made['id']}", headers=dylan.h).json()["ok"] is True
    assert dylan.client.get(made["stream_url"], headers=dylan.h).status_code == 404
