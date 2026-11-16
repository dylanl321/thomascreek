"""A room plays through a login that still has a free slot."""

from __future__ import annotations

from datetime import datetime, timezone

from hub.iptvcanon import observe
from hub.iptvplay import note_usage
from hub.routes.home import shape_event

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _token

CANON = "can_play"
EXP = str(int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()))


def _ready(hub, home_client):
    observe(hub.db, [
        {"provider": "alpha", "kind": "movie", "stream_id": "100", "tmdb": "603", "year": 1999,
         "name": "The Matrix 4K"},
        {"provider": "beta", "kind": "movie", "stream_id": "55", "tmdb": "603", "year": 1999,
         "name": "The Matrix HD"},
    ])
    item = next(row for row in hub.db.kv_get("tv.catalog.canonical")["items"] if row.get("kind") == "movie")
    hub.config.iptv.providers = []
    add = hub.vault.set
    add("iptv.alpha.house.password", "secret-house")
    add("iptv.beta.spare.password", "secret-spare")
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "house", "server": "http://alpha.test", "username": "house",
         "password_secret": "iptv.alpha.house.password"},
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare",
         "password_secret": "iptv.beta.spare.password"},
    ])
    note_usage(hub, "alpha", "house", {"max_connections": "1", "active_cons": "0", "exp_date": EXP, "status": "Active"})
    note_usage(hub, "beta", "spare", {"max_connections": "1", "active_cons": "0", "exp_date": "0", "status": "Active"})
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    token = _token(home_client, key, Clock())["access_token"]
    return item["id"], _auth(token)


def test_a_free_login_is_chosen_and_a_failure_moves_on(hub, people, home_client, away_client):
    canonical, tv_headers = _ready(hub, home_client)
    dylan = people["phones"]["Dylan"]
    listed = dylan.client.get("/v1/tv/accounts", headers=dylan.h).json()
    alpha = next(row for row in listed["logins"] if row["login"] == "house")
    assert alpha == {
        "provider": "alpha", "login": "house", "max_connections": 1, "active": 0, "slots_left": 1,
        "expires": "2026-12-01", "status": "ok",
    }
    assert "secret-house" not in dylan.client.get("/v1/tv/accounts", headers=dylan.h).text
    assert people["phones"]["Avery"].client.get("/v1/tv/accounts", headers=people["phones"]["Avery"].h).status_code == 403

    subject = hub.tv_app.subject_for(hub.tv_app.tvs()[TV_ID], TV_ID, None)
    screen = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), None, device_id=f"tv:{TV_ID}", tv=True)
    started = dylan.client.post("/v1/tv/tune", headers=dylan.h, json={"canonical_id": canonical, "device": TV_ID})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["ok"] is True and body["canonical_id"] == canonical
    assert body["stream"]["ref"] == "vod:alpha:100" and body["stream"]["quality"] == "2160p"
    assert body["stream"]["url"] == f"/v1/tv/stream/{body['session']}"
    event = screen.queue.get_nowait()
    assert event["type"] == "tv_tune" and event["data"]["session"] == body["session"]

    opened = home_client.get(body["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.status_code == 302
    assert opened.headers["location"].startswith("http://alpha.test/movie/house/")
    assert "100.mp4" in opened.headers["location"]
    assert away_client.get(body["stream"]["url"], headers=tv_headers, follow_redirects=False).status_code == 403

    failed = home_client.post("/v1/tv/tune/fail", headers=tv_headers, json={"session": body["session"]})
    assert failed.status_code == 200, failed.text
    nxt = failed.json()
    assert nxt["ok"] is True and nxt["stream"]["ref"] == "vod:beta:55"
    assert nxt["session"] != body["session"]
    assert dylan.client.post("/v1/tv/tune", headers=dylan.h, json={"canonical_id": "can_missing", "device": TV_ID}).json() == {
        "ok": False, "message": "That title isn't in the catalog.",
    }


def test_a_login_expiring_soon_is_mentioned_once(hub):
    hub.vault.set("iptv.alpha.house.password", "secret-house")
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "house", "server": "http://alpha.test", "username": "house",
         "password_secret": "iptv.alpha.house.password"},
    ])
    soon = str(int(datetime.now(timezone.utc).timestamp()) + 3 * 86400)
    note_usage(hub, "alpha", "house", {"max_connections": "1", "active_cons": "0", "exp_date": soon, "status": "Active"})
    note_usage(hub, "alpha", "house", {"max_connections": "1", "active_cons": "0", "exp_date": soon, "status": "Active"})
    notices = hub.db.kv_get("iptv.notices") or {}
    assert len(notices) == 1


def test_a_tv_learns_which_copies_are_one_title(hub, home_client):
    canonical, tv_headers = _ready(hub, home_client)
    observe(hub.db, [
        {"provider": "alpha", "kind": "movie", "stream_id": "100", "tmdb": "603", "year": 1999,
         "name": "The Matrix 4K"},
        {"provider": "beta", "kind": "movie", "stream_id": "55", "tmdb": "603", "year": 1999,
         "name": "The Matrix HD"},
        {"provider": "alpha", "kind": "movie", "stream_id": "300", "tmdb": "999", "year": 2001,
         "name": "Only Once HD"},
    ])
    body = home_client.get("/v1/tv/copies", headers=tv_headers).json()
    assert body["groups"] == [{"id": canonical, "refs": ["vod:alpha:100", "vod:beta:55"]}]
    assert len(body["version"]) == 12
    assert home_client.get("/v1/tv/copies", headers=tv_headers).json()["version"] == body["version"]
    assert home_client.get("/v1/tv/copies").status_code in (401, 403)


def test_a_film_opens_under_its_own_file_type(hub, people, home_client, monkeypatch):
    """The provider serves a film only under its real extension: an .mkv film asked for as .mp4 stays black."""
    canonical, tv_headers = _ready(hub, home_client)
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda ref: {"id": ref, "ext": "mkv"} if ref == "vod:alpha:100" else None)
    dylan = people["phones"]["Dylan"]
    started = dylan.client.post("/v1/tv/tune", headers=dylan.h, json={"canonical_id": canonical, "device": TV_ID})
    assert started.status_code == 200, started.text
    opened = home_client.get(started.json()["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.status_code == 302
    assert opened.headers["location"].endswith("/100.mkv")


def test_the_first_login_stays_after_a_second_is_saved(hub):
    from hub.iptvplay import logins

    hub.config.iptv.providers = []
    hub.vault.set("iptv.xtream_server", "http://first.test/")
    hub.vault.set("iptv.xtream_username", "house")
    hub.vault.set("iptv.xtream_password", "secret-first")
    hub.db.kv_set("iptv.logins", [
        {"provider": "thomas-creek", "login": "spare", "server": "http://first.test", "username": "spare",
         "password_secret": "iptv.thomas-creek.spare.password"},
    ])
    assert sorted(row["login"] for row in logins(hub)) == ["main", "spare"]
    # The same account saved again on the admin page is not counted twice.
    hub.db.kv_set("iptv.logins", [
        {"provider": "thomas-creek", "login": "again", "server": "http://first.test", "username": "house",
         "password_secret": "iptv.xtream_password"},
    ])
    assert [row["login"] for row in logins(hub)] == ["again"]


def test_a_series_episode_plays_through_a_session_and_kids_only_get_kids_episodes(hub, people, home_client):
    _, tv_headers = _ready(hub, home_client)
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(series=[{"series_id": 50, "name": "Cartoon Show", "category_id": "5"},
                                  {"series_id": 60, "name": "Late Show", "category_id": "6"}],
                          series_categories=[{"category_id": "5", "category_name": "Kids"},
                                             {"category_id": "6", "category_name": "Drama"}])
    hub.tv_catalog.store_series({"info": {"name": "Cartoon Show"}, "episodes": {"1": [
        {"id": "900", "episode_num": 1, "title": "Pilot", "container_extension": "mkv"}]}}, "series:thomas-creek:50")
    hub.tv_catalog.store_series({"info": {"name": "Late Show"}, "episodes": {"1": [
        {"id": "950", "episode_num": 1, "title": "Night"}]}}, "series:thomas-creek:60")
    hub.vault.set("iptv.tc.main.password", "secret-main")
    hub.db.kv_set("iptv.logins", hub.db.kv_get("iptv.logins") + [
        {"provider": "thomas-creek", "login": "main", "server": "http://tc.test", "username": "main",
         "password_secret": "iptv.tc.main.password"}])
    note_usage(hub, "thomas-creek", "main", {"max_connections": "2", "active_cons": "0", "exp_date": EXP, "status": "Active"})

    dylan = people["phones"]["Dylan"]
    started = dylan.client.post("/v1/tv/tune", headers=dylan.h,
                                json={"episode_id": "ep:thomas-creek:900", "device": TV_ID}).json()
    assert started["ok"] is True and started["stream"]["ref"] == "ep:thomas-creek:900"
    opened = home_client.get(started["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.status_code == 302 and "/series/main/" in opened.headers["location"]
    assert dylan.client.post("/v1/tv/tune", headers=dylan.h,
                             json={"canonical_id": "ep:thomas-creek:404", "device": TV_ID}).json()["ok"] is False

    theo = people["phones"]["Theo"]
    late = theo.client.post("/v1/tv/tune", headers=theo.h, json={"episode_id": "ep:thomas-creek:950", "device": TV_ID})
    assert late.status_code in (200, 403)
    if late.status_code == 200:
        assert late.json() == {"ok": False, "message": "That show isn't in the kids' library."}
