"""#204: the Now playing Live Activity is started, updated and ended by the hub, and can be switched off."""

from __future__ import annotations

import asyncio

from hub import nowplaying_live

from tests.test_tvs import _tv


class FakeApns:
    enabled = True

    def __init__(self):
        self.sent = []

    async def close(self):
        pass

    async def send(self, token, payload, **kw):
        self.sent.append((token, payload, kw))

        class R:
            ok, token_dead = True, False
        return R()


def _playing(home_client, headers, title="Bluey", person=None):
    body = {"foreground": True, "playing": {"title": title, "kind": "episode"} if title else None}
    if person:
        body["person_id"] = person
    assert home_client.post("/v1/tv/state", headers=headers, json=body).status_code == 200


def _starts(apns):
    return sorted(t for t, p, _ in apns.sent if p["aps"]["event"] == "start")


def test_start_update_end_and_the_opt_out(hub, home_client, people, monkeypatch):
    monkeypatch.setattr(nowplaying_live, "kick", lambda h: None)  # sync by hand, step by step
    _, tv_headers = _tv(hub, home_client)
    apns = FakeApns()
    hub.apns = apns  # type: ignore[assignment]
    dylan, avery, theo = people["phones"]["Dylan"], people["phones"]["Avery"], people["phones"]["Theo"]
    for ph, tok in ((dylan, "aa11"), (avery, "aa22"), (theo, "aa33")):
        r = home_client.post("/v1/devices/live-activity", headers=ph.h,
                             json={"now_playing_start_token": tok, "env": "sandbox"})
        assert r.status_code == 200, r.text

    # Nobody named on the TV: nobody's phone hears about it.
    _playing(home_client, tv_headers, "Bluey")
    asyncio.run(nowplaying_live.sync(hub))
    assert apns.sent == []

    # Playing under Dylan's name: only Dylan's phone.
    _playing(home_client, tv_headers, "Bluey", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    assert _starts(apns) == ["aa11"]
    payload = [p for t, p, _ in apns.sent if t == "aa11"][0]["aps"]
    assert payload["attributes-type"] == "NowPlayingAttributes"
    assert payload["content-state"]["tvs"] == [{"id": payload["content-state"]["tvs"][0]["id"], "name": "Living room",
                                                "title": "Bluey", "kind": "episode"}]
    assert payload["content-state"]["title"] == "Bluey" and payload["content-state"]["room"] == "Living room"
    assert apns.sent[0][2]["push_type"] == "liveactivity"

    # Not started twice while the phone hasn't sent its activity token yet.
    apns.sent.clear()
    asyncio.run(nowplaying_live.sync(hub))
    assert apns.sent == []

    # Dylan's phone sends the running activity's token: a change is an update, the same state sends nothing.
    home_client.post("/v1/devices/live-activity", headers=dylan.h,
                     json={"now_playing_activity_token": "bb22", "activity_id": "act1", "env": "sandbox"})
    _playing(home_client, tv_headers, "Bluey", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    assert apns.sent == []
    _playing(home_client, tv_headers, "Peppa", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    updates = [(t, p) for t, p, _ in apns.sent if p["aps"]["event"] == "update"]
    assert updates and updates[0][0] == "bb22" and updates[0][1]["aps"]["content-state"]["title"] == "Peppa"

    # The TV moves to Avery: Dylan's activity ends, Avery's phone starts.
    apns.sent.clear()
    _playing(home_client, tv_headers, "Peppa", people["avery"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    assert [t for t, p, _ in apns.sent if p["aps"]["event"] == "end"] == ["bb22"]
    assert _starts(apns) == ["aa22"]

    # The TV stops.
    _playing(home_client, tv_headers, None)
    asyncio.run(nowplaying_live.sync(hub))

    # Dylan switches it off: a show under his name leaves his phone alone.
    r = home_client.put("/v1/alerts", headers=dylan.h, json={"prefs": {"now_playing": False}})
    assert r.status_code == 200, r.text
    assert hub.alerts.prefs(people["dylan"]["id"])["now_playing"] is False
    apns.sent.clear()
    _playing(home_client, tv_headers, "Bluey", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    assert _starts(apns) == []
    assert hub.db.kv_get(f"la_np.{dylan.device_id}") is None


def test_a_child_only_sees_tvs_they_may_control(hub, home_client, people, monkeypatch):
    monkeypatch.setattr(nowplaying_live, "kick", lambda h: None)
    _tv(hub, home_client)
    hub.tv_app.set_tv(next(iter(hub.tv_app.tvs())), room="office")
    apns = FakeApns()
    hub.apns = apns  # type: ignore[assignment]
    theo = people["phones"]["Theo"]
    tv = hub.tv_app.tvs()
    install_id = next(iter(tv))
    tv[install_id].update(playing={"title": "Late film"}, state_at=__import__("time").time(),
                          watcher=people["theo"]["id"])
    hub.tv_app._save_tvs(tv)
    assert nowplaying_live.tvs_for(hub, hub.db.person(people["theo"]["id"])) == []
    assert nowplaying_live.tvs_for(hub, hub.db.person(people["dylan"]["id"])) == []  # not under his name
    tv = hub.tv_app.tvs()
    tv[install_id]["watcher"] = people["dylan"]["id"]
    hub.tv_app._save_tvs(tv)
    assert nowplaying_live.tvs_for(hub, hub.db.person(people["dylan"]["id"]))[0]["title"] == "Late film"
    home_client.post("/v1/devices/live-activity", headers=theo.h, json={"now_playing_start_token": "cc33"})
    asyncio.run(nowplaying_live.sync(hub))
    assert not [t for t, _p, _ in apns.sent if t == "cc33"]


def test_a_start_that_never_came_back_alerts_once(hub, home_client, people, monkeypatch):
    """Nov 21 2026: a TV on one channel re-alerted every phone in the house every ten minutes all afternoon."""
    monkeypatch.setattr(nowplaying_live, "kick", lambda h: None)
    _, tv_headers = _tv(hub, home_client)
    apns = FakeApns()
    hub.apns = apns  # type: ignore[assignment]
    dylan = people["phones"]["Dylan"]
    home_client.post("/v1/devices/live-activity", headers=dylan.h, json={"now_playing_start_token": "aa11"})

    def starts():
        return [t for t, p, _ in apns.sent if p["aps"]["event"] == "start" and t == "aa11"]

    t0 = 1_800_000_000.0
    _playing(home_client, tv_headers, "AR Sport 1", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub, now=t0))
    assert starts() == ["aa11"]
    # The phone never answers with its activity token. Hours on the same channel: no second alert.
    for k in range(1, 5):
        asyncio.run(nowplaying_live.sync(hub, now=t0 + k * 600))
    # The stream drops and comes back: still no second alert.
    _playing(home_client, tv_headers, None)
    asyncio.run(nowplaying_live.sync(hub, now=t0 + 3_000))
    _playing(home_client, tv_headers, "AR Sport 1", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub, now=t0 + 3_100))
    assert starts() == ["aa11"]
    # Something else on, soon after: it waits.
    _playing(home_client, tv_headers, "Bluey", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub, now=t0 + 3_200))
    assert starts() == ["aa11"]
    # Something else, hours later: one new start.
    asyncio.run(nowplaying_live.sync(hub, now=t0 + nowplaying_live.START_WAIT_S + 1))
    assert starts() == ["aa11", "aa11"]


def test_a_test_emulator_playing_as_someone_starts_nothing_on_their_phone(hub, home_client, people, monkeypatch):
    """An Android emulator or Apple simulator on the Mac, playing as Dylan for a walk-through, never starts his Now
    playing activity (Nov 21 2026: one stuck in his Dynamic Island he couldn't clear)."""
    monkeypatch.setattr(nowplaying_live, "kick", lambda h: None)
    _, tv_headers = _tv(hub, home_client)
    apns = FakeApns()
    hub.apns = apns  # type: ignore[assignment]
    dylan = people["phones"]["Dylan"]
    home_client.post("/v1/devices/live-activity", headers=dylan.h,
                     json={"now_playing_start_token": "aa11", "env": "sandbox"})
    tv_id = next(iter(hub.tv_app.tvs()))
    for model in ("sdk_google_atv64_arm64", "arm64"):
        _model(hub, tv_id, model)
        _playing(home_client, tv_headers, "US| ABC News", people["dylan"]["id"])
        asyncio.run(nowplaying_live.sync(hub))
        assert apns.sent == []
    _model(hub, tv_id, "SHIELD Android TV")
    _playing(home_client, tv_headers, "US| ABC News", people["dylan"]["id"])
    asyncio.run(nowplaying_live.sync(hub))
    assert _starts(apns) == ["aa11"]


def _model(hub, tv_id, model):
    tvs = hub.tv_app.tvs()
    tvs[tv_id]["model"] = model
    hub.tv_app._save_tvs(tvs)
