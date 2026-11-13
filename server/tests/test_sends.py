"""Grown-ups sending films, shows and YouTube to the kids' iPads (hub/sends.py, routes/sends.py)."""

from __future__ import annotations

import asyncio

import pytest
import respx

from hub.downloads import Downloads
from hub.sends import pick_episodes, prep_device

from .test_youtube import _drain, yt  # noqa: F401  (fixture)

PLEX = "http://plex.test"


def _ep(n: int, season: int, ep: int):
    return {"ratingKey": str(n), "type": "episode", "title": f"Ep {ep}", "grandparentTitle": "Bluey",
            "parentIndex": season, "index": ep, "duration": 420_000, "thumb": f"/t/{n}"}


EPS = [_ep(101, 1, 1), _ep(102, 1, 2), _ep(103, 1, 3), _ep(201, 2, 1), _ep(202, 2, 2), _ep(203, 2, 3)]


def _movie(key="42", rating="R"):
    return {"MediaContainer": {"Metadata": [{
        "ratingKey": key, "type": "movie", "title": "Big Film", "year": 2024, "contentRating": rating, "duration": 6_000_000,
        "thumb": "/t/42",
        "Media": [{"videoCodec": "h264", "audioCodec": "aac", "container": "mp4", "videoResolution": "1080",
                   "Part": [{"key": "/library/parts/7/1/file.mp4", "size": 2_000_000_000, "container": "mp4"}]}]}]}}


@pytest.fixture
def plex(hub, tmp_path):
    hub.config.plex.enabled = True
    hub.config.plex.url = PLEX
    hub.vault.set("plex.token", "tok")
    hub.downloads = Downloads(hub, tmp_path / "downloads")
    return hub


def _mock_show(mock):
    mock.get(f"{PLEX}/library/metadata/50").respond(json={"MediaContainer": {"Metadata": [{
        "ratingKey": "50", "type": "show", "title": "Bluey", "contentRating": "TV-Y", "thumb": "/t/50"}]}})
    mock.get(f"{PLEX}/library/metadata/50/allLeaves").respond(json={"MediaContainer": {"Metadata": list(reversed(EPS))}})
    mock.get(f"{PLEX}/:/scrobble").respond(200)


def _ids(phone):
    r = phone.client.get("/v1/sends/mine", headers=phone.h)
    assert r.status_code == 200, r.text
    return [i["item_id"] for i in r.json()["items"]]


def test_pick_episodes():
    eps = [{"id": str(i), "season": 1 + i // 3, "episode": i % 3} for i in range(6)]
    assert [e["id"] for e in pick_episodes(eps, "next", 2, None, {})] == ["0", "1"]
    assert [e["id"] for e in pick_episodes(eps, "next", 2, None, {"0": 1, "3": 2})] == ["4", "5"]
    assert [e["id"] for e in pick_episodes(eps, "last", 2, None, {})] == ["4", "5"]
    assert [e["id"] for e in pick_episodes(eps, "season", None, None, {})] == ["3", "4", "5"]
    assert [e["id"] for e in pick_episodes(eps, "season", None, 1, {})] == ["0", "1", "2"]
    assert len(pick_episodes(eps, "all", None, None, {})) == 6
    assert pick_episodes(eps, "next", 3, None, {"5": 1}) == []


def test_show_next_episodes_move_along_as_the_kid_watches(plex, people, hub):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        _mock_show(mock)
        # Kids can't send.
        assert theo.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "show", "ref": "50"},
                                headers=theo.h).status_code == 403
        r = dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "title", "ref": "50",
                                                 "scope": "next", "count": 2}, headers=dylan.h)
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Bluey"
        s = hub.sends.all(people["theo"]["id"])[0]
        assert s["kind"] == "show" and s["scope"] == "next" and s["count"] == 2   # a show id sent as a title is a show
        assert _ids(theo) == ["101", "102"]
        item = theo.client.get("/v1/sends/mine", headers=theo.h).json()["items"][0]
        assert item["from"] == "Dylan" and item["quality"] == "high" and item["season"] == 1 and item["ready"] is True
        # Watching episode 1 on the iPad moves the window on.
        assert theo.client.post("/v1/media/101/progress", json={"offset_s": 400, "watched": True}, headers=theo.h).status_code == 200
        assert _ids(theo) == ["102", "103"]
        # Watched reported in the inventory counts too.
        theo.client.post("/v1/sends/inventory", json={"items": [{"item_id": "103", "state": "ready", "watched": True}]},
                         headers=theo.h)
        assert _ids(theo) == ["201", "202"]
        # The same show again changes how much goes rather than adding a second send.
        dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "show", "ref": "50",
                                             "scope": "season"}, headers=dylan.h)
        assert len(hub.sends.all(people["theo"]["id"])) == 1
        assert _ids(theo) == ["201", "202", "203"]
        sid = hub.sends.all(people["theo"]["id"])[0]["id"]
        assert dylan.client.patch(f"/v1/sends/{sid}", json={"scope": "last", "count": 1}, headers=dylan.h).status_code == 200
        assert _ids(theo) == ["203"]
        assert dylan.client.patch(f"/v1/sends/{sid}", json={"scope": "all"}, headers=dylan.h).status_code == 200
        assert len(_ids(theo)) == 6
        # Removing it empties the lineup; the iPad deletes what it brought.
        assert dylan.client.delete(f"/v1/sends/{sid}", headers=dylan.h).status_code == 200
        r = theo.client.get("/v1/sends/mine", headers=theo.h).json()
        assert r == {"items": [], "active": False}


def test_a_send_is_permission_beyond_the_kids_ratings(plex, people, hub):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_movie(rating="R"))
        mock.get(f"{PLEX}/library/metadata/43").respond(json=_movie(key="43", rating="R"))
        assert theo.client.post("/v1/media/42/download", json={"quality": "original"}, headers=theo.h).status_code == 404
        r = dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "title", "ref": "42",
                                                 "quality": "original"}, headers=dylan.h)
        assert r.status_code == 200, r.text
        j = theo.client.post("/v1/media/42/download", json={"quality": "original"}, headers=theo.h)
        assert j.status_code == 200 and j.json()["status"] == "ready"
        # Only what was sent.
        assert theo.client.post("/v1/media/43/download", json={"quality": "original"}, headers=theo.h).status_code == 404


def test_hub_prepares_ahead_and_devices_share_the_file(plex, people, hub):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    tid = people["theo"]["id"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_movie(rating="PG"))
        dylan.client.post("/v1/sends", json={"person_ids": [tid], "kind": "title", "ref": "42", "quality": "original"},
                          headers=dylan.h)
        res = asyncio.run(hub.sends.sync(tid))
        assert res["items"] == 1 and res["prepared"] == 1
        prep = [j for j in hub.downloads.jobs.values() if j.device_id == prep_device(tid)]
        assert len(prep) == 1 and prep[0].status == "ready" and prep[0].keep_s >= 7 * 86400
        # Running it again doesn't prepare it twice.
        assert asyncio.run(hub.sends.sync(tid))["prepared"] == 0
        j = theo.client.post("/v1/media/42/download", json={"quality": "original"}, headers=theo.h).json()
        assert j["status"] == "ready" and j["id"] != prep[0].id
        assert hub.downloads.get(j["id"]).passthrough == prep[0].passthrough


def test_shared_file_is_kept_until_the_last_job_goes(plex, tmp_path):
    from hub.downloads import Job

    d = plex.downloads
    d.root.mkdir(parents=True)
    f = d.root / "x.mp4"
    f.write_bytes(b"x")
    a = Job(id="a", item_id="1", quality="high", device_id="send:p", person_id="p", item={}, status="ready", path=f,
            keep_s=10, created_at=0)
    b = Job(id="b", item_id="1", quality="high", device_id="ipad", person_id="p", item={}, status="ready", path=f,
            keep_s=100, created_at=0)
    d.jobs = {"a": a, "b": b}
    d.cleanup(now=50)
    assert "a" not in d.jobs and f.exists()
    d.cleanup(now=200)
    assert not d.jobs and not f.exists()


def test_youtube_channel_newest_videos(yt, people, hub):  # noqa: F811
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    yt.feed["videos"] = [{**v, "uploaded": f"202611{10 + i:02d}"} for i, v in enumerate(yt.feed["videos"])]
    r = dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "youtube",
                                             "ref": "https://www.youtube.com/@MarkRober", "count": 2}, headers=dylan.h)
    assert r.status_code == 200, r.text
    src = hub.db.one("SELECT * FROM yt_sources")
    assert src["follow"] == 1 and src["latest"] == 2
    items = theo.client.get("/v1/sends/mine", headers=theo.h).json()["items"]
    # Newest first, not saved on the hub yet.
    assert [i["item_id"] for i in items] == ["yt-vid00000002", "yt-vid00000001"]
    assert all(i["ready"] is False and i["quality"] == "original" for i in items)
    _drain(yt)
    hub.sends.changed(people["theo"]["id"])
    items = theo.client.get("/v1/sends/mine", headers=theo.h).json()["items"]
    assert all(i["ready"] for i in items)
    # A single video as a title.
    r = dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "title", "ref": "yt-vid00000000"},
                          headers=dylan.h)
    assert r.status_code == 200, r.text
    assert len(_ids(theo)) == 3


def test_request_waits_then_becomes_a_send(plex, people, hub, monkeypatch):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    hub.config.jellyseerr.enabled = True
    hub.config.jellyseerr.url = "http://seerr.test"
    info = {"mediaInfo": {"status": 3}}

    async def details(kind, tmdb, fresh=False):
        return info

    monkeypatch.setattr(hub.jellyseerr, "details", details)
    r = dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "request", "ref": "movie:99",
                                             "title": "Moana 2"}, headers=dylan.h)
    assert r.status_code == 200, r.text
    assert _ids(theo) == []
    over = dylan.client.get("/v1/sends", headers=dylan.h).json()["people"][0]
    assert over["sends"][0]["state"] == "waiting"
    info["mediaInfo"] = {"status": 5, "ratingKey": 42}
    hub.sends.changed(people["theo"]["id"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/library/metadata/42").respond(json=_movie(rating="PG"))
        assert _ids(theo) == ["42"]
    s = hub.sends.all(people["theo"]["id"])[0]
    assert s["kind"] == "title" and s["ref"] == "42"


def test_overview_shows_whats_on_each_ipad(plex, people, hub):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        _mock_show(mock)
        dylan.client.post("/v1/sends", json={"person_ids": [people["theo"]["id"]], "kind": "show", "ref": "50",
                                             "scope": "next", "count": 2}, headers=dylan.h)
        theo.client.post("/v1/sends/inventory", json={
            "items": [{"item_id": "101", "state": "ready", "bytes": 100, "sent": True},
                      {"item_id": "102", "state": "downloading", "progress": 0.5},
                      {"item_id": "77", "state": "ready", "bytes": 50}],
            "free_bytes": 5_000, "total_bytes": 64_000}, headers=theo.h)
        r = dylan.client.get("/v1/sends", headers=dylan.h)
        assert r.status_code == 200, r.text
        assert theo.client.get("/v1/sends", headers=theo.h).status_code == 403
    p = r.json()["people"][0]
    assert p["person"]["name"] == "Theo"
    dev = p["devices"][0]
    assert dev["free_bytes"] == 5_000 and dev["used_bytes"] == 150 and dev["sent_bytes"] == 100
    send = p["sends"][0]
    assert send["from"] == "Dylan" and send["state"] == "ready"
    assert [(i["item_id"], i["on"][dev["id"]]) for i in send["items"]] == [("101", "ready"), ("102", "downloading")]


def test_lineup_change_wakes_the_ipads_once(plex, people, hub, monkeypatch):
    dylan = people["phones"]["Dylan"]
    tid = people["theo"]["id"]
    theo_dev = people["phones"]["Theo"].device_id
    hub.db.update("devices", "id", theo_dev, {"apns_token": "abc"})
    sent = []

    class Res:
        ok, token_dead = True, False

    async def send(token, payload, **kw):
        sent.append((token, payload, kw))
        return Res()

    monkeypatch.setattr(type(hub.apns), "enabled", property(lambda s: True))
    monkeypatch.setattr(hub.apns, "send", send)
    with respx.mock(assert_all_called=False) as mock:
        _mock_show(mock)
        dylan.client.post("/v1/sends", json={"person_ids": [tid], "kind": "show", "ref": "50", "scope": "last", "count": 1},
                          headers=dylan.h)
        asyncio.run(hub.sends.sync(tid))
        asyncio.run(hub.sends.sync(tid))
    assert len(sent) == 1
    token, payload, kw = sent[0]
    assert token == "abc" and payload["aps"] == {"content-available": 1} and kw["push_type"] == "background" and kw["priority"] == 5
    # "Top up now" always wakes them.
    with respx.mock(assert_all_called=False) as mock:
        _mock_show(mock)
        assert dylan.client.post("/v1/sends/sync", json={"person_id": tid}, headers=dylan.h).status_code == 200
    assert len(sent) == 2
