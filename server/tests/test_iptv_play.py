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


def test_a_second_provider_is_read_folded_in_and_its_logins_take_the_overflow(hub, people, home_client):
    """A different provider added on the admin page: its lists are read, a film both carry becomes one title
    with both copies, and a TV goes to the second provider when the main login is full."""
    import asyncio

    import httpx
    import respx

    from hub import iptvcanon, iptvplay

    hub.config.iptv.providers = []
    hub.vault.set("iptv.xtream_server", "http://main.test")
    hub.vault.set("iptv.xtream_username", "house")
    hub.vault.set("iptv.xtream_password", "secret-main")
    iptvplay.add_provider(hub, "Other TV", "http://other.test", "spare", "secret-other")
    hub.tv_catalog.ingest(vod=[{"stream_id": 100, "name": "EN - The Matrix (1999)", "tmdb": "603", "container_extension": "mp4"}],
                          vod_categories=[], live=[{"stream_id": 7, "name": "US: ESPN HD", "epg_channel_id": "espn.us"}],
                          live_categories=[], series=[], series_categories=[])
    with respx.mock(assert_all_called=False) as mock:
        def answer(request):
            action = request.url.params.get("action")
            return httpx.Response(200, json={
                "get_vod_streams": [{"stream_id": 55, "name": "The Matrix 1999 FHD", "tmdb": "603", "container_extension": "mkv"}],
                "get_live_streams": [{"stream_id": 9, "name": "ESPN", "epg_channel_id": "espn.us"}],
                "get_series": [],
            }[action])
        mock.get("http://other.test/player_api.php").mock(side_effect=answer)
        got = asyncio.run(hub.tv_catalog.refresh_extras())
    assert got == {"Other-TV": "1 channels, 1 films, 0 series"}
    assert "secret-other" not in hub.tv_catalog._extra_path("Other-TV").read_text()
    film = next(i for i in hub.db.kv_get("tv.catalog.canonical")["items"] if i.get("kind") == "movie")
    assert {s["provider"] for s in film["sources"]} == {"thomas-creek", "Other-TV"}
    channel = next(i for i in hub.db.kv_get("tv.catalog.canonical")["items"] if i.get("kind") == "live")
    assert {s["provider"] for s in channel["sources"]} == {"thomas-creek", "Other-TV"}
    # The page the TV opens is still the main provider's.
    assert iptvcanon.playable(hub.db, kind="movie", title="The Matrix", tmdb="603")["ref"] == "vod:thomas-creek:100"

    # The main login is full: the TV gets the other provider's copy, under its own file type.
    iptvplay.note_usage(hub, "thomas-creek", "main", {"max_connections": "1", "active_cons": "1", "status": "Active"})
    iptvplay.note_usage(hub, "Other-TV", "1", {"max_connections": "1", "active_cons": "0", "status": "Active"})
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    tv_headers = _auth(_token(home_client, key, Clock())["access_token"])
    dylan = people["phones"]["Dylan"]
    started = dylan.client.post("/v1/tv/tune", headers=dylan.h, json={"canonical_id": film["id"], "device": TV_ID}).json()
    assert started["ok"] is True and started["stream"]["ref"] == "vod:Other-TV:55"
    opened = home_client.get(started["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.headers["location"].startswith("http://other.test/movie/spare/") and opened.headers["location"].endswith("/55.mkv")

    # Removing the provider takes its copies out at the next fold.
    iptvplay.remove_provider(hub, "Other-TV")
    hub.tv_catalog.fold()
    film = next(i for i in hub.db.kv_get("tv.catalog.canonical")["items"] if i.get("kind") == "movie")
    assert {s["provider"] for s in film["sources"]} == {"thomas-creek"}


def test_a_fresh_main_list_still_reads_a_provider_added_since(hub):
    """After a restart the main list is fresh, so refresh() doesn't run; the other provider is read anyway."""
    import asyncio

    from hub import iptvplay

    hub.config.iptv.providers = []
    hub.vault.set("iptv.xtream_server", "http://main.test")
    hub.vault.set("iptv.xtream_username", "house")
    hub.vault.set("iptv.xtream_password", "secret-main")
    hub.tv_catalog.ingest(xmltv=b"<tv></tv>")
    doc = hub.tv_catalog.load()
    doc["guide_at"] = doc["fetched_at"]
    hub.tv_catalog._write(doc)
    iptvplay.add_provider(hub, "Other", "http://other.test", "spare", "pw")
    calls = []

    async def fake(**kw):
        calls.append(1)
        return {}
    hub.tv_catalog.refresh_extras = fake  # type: ignore[method-assign]

    async def go():
        await hub.tv_catalog.ensure()
        await hub.tv_catalog.ensure()  # a second request while the first read runs starts no other
        await asyncio.sleep(0)
    asyncio.run(go())
    assert calls == [1]


def test_a_title_only_a_second_provider_carries_has_its_own_pages(hub, people, home_client):
    """#207: a film and a series that only another provider lists open a page with the same fields as the main
    provider's, read with that provider's own login; its episodes play from that provider; kids never see it."""
    import asyncio

    import httpx
    import respx

    from hub import iptvplay
    from hub.tvexplore import _canonical_rows

    hub.config.iptv.providers = []
    hub.vault.set("iptv.xtream_server", "http://main.test")
    hub.vault.set("iptv.xtream_username", "house")
    hub.vault.set("iptv.xtream_password", "secret-main")
    iptvplay.add_provider(hub, "Other TV", "http://other.test", "spare", "secret-other")
    hub.tv_catalog.ingest(vod=[], vod_categories=[], live=[], live_categories=[], series=[], series_categories=[],
                          xmltv=b"<tv></tv>")
    doc = hub.tv_catalog.load()
    doc["guide_at"] = doc["fetched_at"]
    hub.tv_catalog._write(doc)
    asked = []
    with respx.mock(assert_all_called=False) as mock:
        def answer(request):
            action = request.url.params.get("action")
            asked.append((request.url.params.get("username"), action))
            return httpx.Response(200, json={
                "get_vod_streams": [{"stream_id": 77, "name": "Only Here (2021)", "container_extension": "mkv"}],
                "get_live_streams": [],
                "get_series": [{"series_id": 31, "name": "Rare Show"}],
                "get_vod_info": {"info": {"name": "Only Here", "plot": "A film one provider has.",
                                          "movie_image": "https://image.tmdb.org/t/p/w500/77.jpg", "duration_secs": 5400}},
                "get_series_info": {"info": {"name": "Rare Show", "plot": "Only on the second provider."},
                                    "episodes": {"1": [{"id": "3101", "episode_num": 1, "title": "Start",
                                                        "container_extension": "mkv"}]}},
            }[action])
        mock.get("http://other.test/player_api.php").mock(side_effect=answer)
        asyncio.run(hub.tv_catalog.refresh_extras())

        dylan = people["phones"]["Dylan"]
        film = dylan.client.get("/v1/tv/movies/vod:Other-TV:77", headers=dylan.h)
        assert film.status_code == 200, film.text
        body = film.json()
        assert body["id"] == "vod:Other-TV:77" and body["plot"] == "A film one provider has."
        show = dylan.client.get("/v1/tv/series/series:Other-TV:31", headers=dylan.h)
        assert show.status_code == 200, show.text
        episode = show.json()["seasons"][0]["episodes"][0]
        assert episode["id"] == "ep:Other-TV:3101" and episode["title"] == "Start"
        for text in (film.text, show.text):
            assert "secret-other" not in text and "spare" not in text and "other.test" not in text
        # The other provider's own login opened the pages; the main login wasn't used for them.
        assert ("spare", "get_vod_info") in asked and ("spare", "get_series_info") in asked
        assert all(user == "spare" for user, _ in asked)

        # A kid gets neither page.
        theo = people["phones"]["Theo"]
        assert theo.client.get("/v1/tv/movies/vod:Other-TV:77", headers=theo.h).status_code in (403, 404)

    # Explore can open the title now: its row points at the other provider's page.
    rows = {row["title"]: row for row in _canonical_rows(hub)}
    assert any(row.get("movie_id") == "vod:Other-TV:77" for row in rows.values())

    # The episode plays from the provider that carries it.
    iptvplay.note_usage(hub, "Other-TV", "1", {"max_connections": "1", "active_cons": "0", "status": "Active"})
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room")
    tv_headers = _auth(_token(home_client, key, Clock())["access_token"])
    started = dylan.client.post("/v1/tv/tune", headers=dylan.h,
                                json={"episode_id": "ep:Other-TV:3101", "device": TV_ID}).json()
    assert started["ok"] is True and started["stream"]["ref"] == "ep:Other-TV:3101"
    opened = home_client.get(started["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.headers["location"].startswith("http://other.test/series/spare/")
    assert opened.headers["location"].endswith("/3101.mkv")


def test_a_tv_that_stopped_frees_its_login(hub, people, home_client, monkeypatch):
    """The heartbeat keeps a login only while something plays (Dylan, Nov 2026: one stream per login)."""
    import hub.iptvplay as iptvplay

    canonical, tv_headers = _ready(hub, home_client)
    started = home_client.post("/v1/tv/tune", headers=tv_headers, json={"canonical_id": canonical})
    assert started.status_code == 200 and started.json()["ok"], started.text
    real = iptvplay.time.time
    monkeypatch.setattr(iptvplay.time, "time", lambda: real() + 60)
    home_client.post("/v1/tv/state", headers=tv_headers,
                     json={"playing": {"name": "The Matrix", "title": "The Matrix"}})
    monkeypatch.setattr(iptvplay.time, "time", lambda: real() + 120)
    home_client.post("/v1/tv/state", headers=tv_headers, json={"foreground": True})
    iptvplay._release_idle(hub, real() + 120)
    assert [row for row in hub.db.kv_get("iptv.sessions") if row["tv"] == TV_ID]
    monkeypatch.setattr(iptvplay.time, "time", lambda: real() + 200)
    iptvplay._release_idle(hub, real() + 200)
    assert not [row for row in hub.db.kv_get("iptv.sessions") if row["tv"] == TV_ID]


def test_a_refused_channel_goes_to_the_other_login_for_hours(hub):
    """Nov 21 2026: one login was refused a sports channel (405) while a second login of the same provider played it.
    The refused pair stays aside for hours when another login can take it, minutes when it is the only one."""
    from hub import iptvplay

    hub.config.iptv.providers = []
    hub.vault.set("iptv.alpha.one.password", "p1")
    hub.vault.set("iptv.alpha.two.password", "p2")
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "one", "server": "http://alpha.test", "username": "one",
         "password_secret": "iptv.alpha.one.password"},
        {"provider": "alpha", "login": "two", "server": "http://alpha.test", "username": "two",
         "password_secret": "iptv.alpha.two.password"},
    ])
    ref = "xc:alpha:77"
    first = iptvplay.lease(hub, ref, "phone:a", now=1000.0)
    assert first is not None
    assert iptvplay.refused(hub, "phone:a", now=1001.0)
    second = iptvplay.lease(hub, ref, "phone:a", now=1002.0)
    assert second is not None and second["login"] != first["login"]
    # Still on the other login well after the short failure window.
    iptvplay.release_tv(hub, "phone:a")
    later = iptvplay.lease(hub, ref, "phone:a", now=1001.0 + iptvplay.FAIL_S + 60)
    assert later is not None and later["login"] == second["login"]
    assert not iptvplay.all_refused(hub, ref, now=1003.0)


def _two_logins(hub, backups=None):
    from hub import iptvaddr

    hub.config.iptv.providers = []
    if backups:
        iptvaddr.set_backups(hub.db, "alpha", backups, "http://alpha.test")
    hub.vault.set("iptv.alpha.one.password", "p1")
    hub.vault.set("iptv.alpha.two.password", "p2")
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "one", "server": "http://alpha.test", "username": "one",
         "password_secret": "iptv.alpha.one.password"},
        {"provider": "alpha", "login": "two", "server": "http://alpha.test", "username": "two",
         "password_secret": "iptv.alpha.two.password"},
    ])


def test_an_old_pass_refused_leaves_the_stream_playing_now_alone(hub):
    """Nov 21 2026: a phone's old relay pass was refused after the phone had moved on; the hub then set aside and freed
    the stream the phone was playing now, and the login went to someone else mid-play."""
    from hub import iptvplay

    _two_logins(hub)
    now_playing = iptvplay.lease(hub, "xc:alpha:88", "phone:a", now=1000.0)
    assert now_playing is not None
    pl = f"alpha|{now_playing['login']}"
    other = "alpha|two" if now_playing["login"] == "one" else "alpha|one"
    assert not iptvplay.refused(hub, "phone:a", now=1001.0, login=other, stream="88")  # another login
    assert not iptvplay.refused(hub, "phone:a", now=1001.0, login=pl, stream="77")     # another channel
    assert iptvplay.held_by(hub, "phone:a") is not None
    assert iptvplay.refused(hub, "phone:a", now=1001.0, login=pl, stream="88")
    assert iptvplay.held_by(hub, "phone:a") is None
    assert iptvplay.held_elsewhere(hub, "alpha", now_playing["login"], "phone:b") is False


def test_a_login_held_by_another_player_is_seen(hub):
    from hub import iptvplay

    _two_logins(hub)
    row = iptvplay.lease(hub, "xc:alpha:88", "tv-1", now=1000.0)
    assert row is not None
    assert iptvplay.held_elsewhere(hub, "alpha", row["login"], "phone:a")
    assert not iptvplay.held_elsewhere(hub, "alpha", row["login"], "tv-1")


def test_a_touch_writes_at_most_every_few_seconds(hub, monkeypatch):
    """Each relayed segment touched the lease and wrote the list under the pool lock; the loop stood still on it."""
    from hub import iptvplay

    _two_logins(hub)
    assert iptvplay.lease(hub, "xc:alpha:88", "phone:a") is not None
    writes = []
    real = hub.db.kv_set
    monkeypatch.setattr(hub.db, "kv_set", lambda key, value: (writes.append(key), real(key, value))[1])
    for _ in range(20):
        iptvplay.touch(hub, "phone:a")
    assert writes.count(iptvplay.KV_SESSIONS) == 1


def test_direct_addresses_are_built_once(hub, monkeypatch):
    """lease.urls took near a second: every backup address read the catalog and the vault again."""
    from hub import iptvplay

    _two_logins(hub, backups=["http://alpha-b.test", "http://alpha-c.test"])
    reads = []
    monkeypatch.setattr(hub.tv_catalog, "movie", lambda ref: reads.append(ref) or {"id": ref, "ext": "mkv"})
    urls = iptvplay.direct_all(hub, "vod:alpha:100", "phone:a")
    assert [u.split("/movie/")[0] for u in urls] == ["http://alpha.test", "http://alpha-b.test", "http://alpha-c.test"]
    assert all(u.endswith("/100.mkv") for u in urls)
    assert len({u.split("/movie/")[1] for u in urls}) == 1
    assert len(reads) == 1
    iptvplay.direct_all(hub, "vod:alpha:100", "phone:a")
    assert len(reads) == 1  # remembered


def test_an_unfolded_film_plays_by_its_own_id_and_kids_only_get_kids_films(hub, people, home_client):
    """A film listed once has no folded title yet. A TV plays it by its provider id, as a phone already could."""
    _, tv_headers = _ready(hub, home_client)
    hub.tv_app.set_kids_categories(["Kids"])
    hub.tv_catalog.ingest(vod=[{"stream_id": 700, "name": "Paper Boats", "category_id": "5"},
                               {"stream_id": 710, "name": "Late Film", "category_id": "6"}],
                          vod_categories=[{"category_id": "5", "category_name": "Kids"},
                                          {"category_id": "6", "category_name": "Drama"}])
    hub.vault.set("iptv.tc.main.password", "secret-main")
    hub.db.kv_set("iptv.logins", hub.db.kv_get("iptv.logins") + [
        {"provider": "thomas-creek", "login": "main", "server": "http://tc.test", "username": "main",
         "password_secret": "iptv.tc.main.password"}])
    note_usage(hub, "thomas-creek", "main",
               {"max_connections": "2", "active_cons": "0", "exp_date": EXP, "status": "Active"})

    late_film = {"canonical_id": "vod:thomas-creek:710"}
    started = home_client.post("/v1/tv/tune", headers=tv_headers, json=late_film).json()
    assert started["ok"] is True and started["stream"]["ref"] == "vod:thomas-creek:710"
    opened = home_client.get(started["stream"]["url"], headers=tv_headers, follow_redirects=False)
    assert opened.status_code == 302 and "/movie/main/" in opened.headers["location"]
    assert "secret-main" not in started["stream"]["url"]
    missing = {"canonical_id": "vod:thomas-creek:404"}
    unknown = home_client.post("/v1/tv/tune", headers=tv_headers, json=missing).json()
    assert unknown == {"ok": False, "message": "That title isn't in the catalog."}

    theo = people["phones"]["Theo"]
    late = theo.client.post("/v1/tv/tune", headers=theo.h, json={**late_film, "device": TV_ID})
    assert late.status_code in (200, 403)
    if late.status_code == 200:
        assert late.json() == {"ok": False, "message": "That film isn't in the kids' library."}
