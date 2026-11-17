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


def _playing(home_client, headers, title="Bluey"):
    body = {"foreground": True, "playing": {"title": title, "kind": "episode"} if title else None}
    assert home_client.post("/v1/tv/state", headers=headers, json=body).status_code == 200


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

    _playing(home_client, tv_headers, "Bluey")
    asyncio.run(nowplaying_live.sync(hub))
    starts = [(t, p) for t, p, _ in apns.sent if p["aps"]["event"] == "start"]
    # Dylan and Avery; not Theo, who can't control that TV.
    assert sorted(t for t, _ in starts) == ["aa11", "aa22"]
    payload = starts[0][1]["aps"]
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
    _playing(home_client, tv_headers, "Bluey")
    asyncio.run(nowplaying_live.sync(hub))
    assert apns.sent == []
    _playing(home_client, tv_headers, "Peppa")
    asyncio.run(nowplaying_live.sync(hub))
    updates = [(t, p) for t, p, _ in apns.sent if p["aps"]["event"] == "update"]
    assert updates and updates[0][0] == "bb22" and updates[0][1]["aps"]["content-state"]["title"] == "Peppa"

    # The TV stops: Dylan's activity ends.
    apns.sent.clear()
    _playing(home_client, tv_headers, None)
    asyncio.run(nowplaying_live.sync(hub))
    ends = [(t, p) for t, p, _ in apns.sent if p["aps"]["event"] == "end"]
    assert [t for t, _ in ends] == ["bb22"]

    # Dylan switches it off: the next TV start leaves his phone alone.
    r = home_client.put("/v1/alerts", headers=dylan.h, json={"prefs": {"now_playing": False}})
    assert r.status_code == 200, r.text
    assert hub.alerts.prefs(people["dylan"]["id"])["now_playing"] is False
    apns.sent.clear()
    _playing(home_client, tv_headers, "Bluey")
    asyncio.run(nowplaying_live.sync(hub))
    assert [t for t, p, _ in apns.sent if p["aps"]["event"] == "start"] == ["aa22"]  # Avery only
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
    tv[install_id].update(playing={"title": "Late film"}, state_at=__import__("time").time())
    hub.tv_app._save_tvs(tv)
    assert nowplaying_live.tvs_for(hub, hub.db.person(people["theo"]["id"])) == []
    assert nowplaying_live.tvs_for(hub, hub.db.person(people["dylan"]["id"]))[0]["title"] == "Late film"
    home_client.post("/v1/devices/live-activity", headers=theo.h, json={"now_playing_start_token": "cc33"})
    asyncio.run(nowplaying_live.sync(hub))
    assert not [t for t, _p, _ in apns.sent if t == "cc33"]
