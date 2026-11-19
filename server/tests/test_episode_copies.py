"""An episode plays from another provider's copy of the show when its own provider's logins are busy
(Nov 19 2026: every episode competed for the main provider's two logins, though most shows were folded with a
second provider)."""

from __future__ import annotations

import asyncio

from hub.iptvcanon import observe
from hub.iptvplay import _episode, busy_reason, lease, note_usage, read_twins


def _show(hub):
    observe(hub.db, [
        {"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "EN - Monk",
         "ref": "series:thomas-creek:9"},
        {"provider": "beta", "kind": "series", "stream_id": "44", "name": "Monk", "ref": "series:beta:44"},
    ])
    hub.config.iptv.providers = []
    hub.vault.set("iptv.tc.main.password", "secret-main")
    hub.vault.set("iptv.beta.spare.password", "secret-spare")
    hub.db.kv_set("iptv.logins", [
        {"provider": "thomas-creek", "login": "main", "server": "http://tc.test", "username": "main",
         "password_secret": "iptv.tc.main.password"},
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare",
         "password_secret": "iptv.beta.spare.password"},
    ])
    for pid, login in (("thomas-creek", "main"), ("beta", "spare")):
        note_usage(hub, pid, login, {"max_connections": "1", "active_cons": "0", "status": "Active"})


def _page(ids: dict[tuple[int, int], str], ext: str) -> dict:
    seasons: dict[str, list] = {}
    for (season, number), eid in ids.items():
        seasons.setdefault(str(season), []).append(
            {"id": eid, "episode_num": number, "title": f"Episode {number}", "container_extension": ext})
    return {"info": {"name": "Monk"}, "episodes": seasons}


def test_a_busy_episode_plays_from_the_other_providers_copy(hub):
    _show(hub)
    cat = hub.tv_catalog
    cat.store_series(_page({(1, 1): "501", (1, 2): "502"}, "mkv"), "series:thomas-creek:9")
    assert [s["ref"] for s in _episode(hub, "ep:thomas-creek:502")["sources"]] == ["ep:thomas-creek:502"]

    async def fetch(series_id):
        assert series_id == "series:beta:44"
        return cat.store_series(_page({(1, 1): "7001", (1, 2): "7002"}, "mp4"), series_id)

    cat.fetch_series = fetch
    assert asyncio.run(read_twins(hub, "ep:thomas-creek:502")) == 1
    refs = [s["ref"] for s in _episode(hub, "ep:thomas-creek:502")["sources"]]
    assert refs == ["ep:thomas-creek:502", "ep:beta:7002"]

    first = lease(hub, "ep:thomas-creek:501", "phone:dev1")
    assert first and first["provider"] == "thomas-creek"
    second = lease(hub, "ep:thomas-creek:502", "phone:dev2")
    assert second and second["provider"] == "beta" and second["ref"] == "ep:beta:7002"
    assert lease(hub, "ep:thomas-creek:502", "phone:dev3") is None
    assert "in use" in busy_reason(hub, "ep:thomas-creek:502")


def test_a_show_with_no_other_copy_reads_nothing(hub):
    _show(hub)
    cat = hub.tv_catalog
    observe(hub.db, [{"provider": "thomas-creek", "kind": "series", "stream_id": "10", "name": "Only Here",
                      "ref": "series:thomas-creek:10"}])
    cat.store_series(_page({(1, 1): "601"}, "mp4"), "series:thomas-creek:10")

    async def never(_series_id):
        raise AssertionError("no other copy to read")

    cat.fetch_series = never
    assert asyncio.run(read_twins(hub, "ep:thomas-creek:601")) == 0
