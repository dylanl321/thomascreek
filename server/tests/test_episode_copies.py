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


def test_a_dubbed_copy_of_the_show_is_never_played_instead(hub):
    """Nov 20 2026: Community played in Turkish: its folded twin on another provider had more free logins."""
    from hub.iptvplay import foreign

    assert foreign("TR - Community") and foreign("|DE| Monk") and foreign("[FR] Monk") and foreign("Monk (Dublaj)")
    assert not foreign("EN - Monk") and not foreign("Monk") and not foreign("NF - 13 Bombs")
    assert foreign("Monk (TR)") and foreign("Monk [DE]") and foreign("Monk |TR|")
    assert not foreign("It: Chapter Two") and not foreign("US - The Office") and not foreign("Monk (US)")
    assert not foreign("Monk (2004)") and not foreign("Monk [4K]") and not foreign("Monk (HD)")
    observe(hub.db, [
        {"provider": "thomas-creek", "kind": "series", "stream_id": "9", "name": "EN - Monk", "tmdb": "1695",
         "ref": "series:thomas-creek:9"},
        {"provider": "beta", "kind": "series", "stream_id": "44", "name": "Monk (TR)", "tmdb": "1695",
         "ref": "series:beta:44"},
    ])
    _show_logins(hub, beta_slots="5")
    cat = hub.tv_catalog
    cat.store_series(_page({(1, 1): "501"}, "mkv"), "series:thomas-creek:9")
    cat.store_series(_page({(1, 1): "7001"}, "mp4"), "series:beta:44")
    assert [s["ref"] for s in _episode(hub, "ep:thomas-creek:501")["sources"]] == ["ep:thomas-creek:501",
                                                                                   "ep:beta:7001"]
    first = lease(hub, "ep:thomas-creek:501", "phone:dev1")
    assert first and first["provider"] == "thomas-creek"  # English, though the dubbed copy has 5 free logins
    assert lease(hub, "ep:thomas-creek:501", "phone:dev2") is None  # busy, not a surprise dub
    # Asked for the dubbed copy itself: it plays.
    assert lease(hub, "ep:beta:7001", "phone:dev3")["provider"] == "beta"


def _show_logins(hub, *, beta_slots: str) -> None:
    hub.config.iptv.providers = []
    hub.vault.set("iptv.tc.main.password", "secret-main")
    hub.vault.set("iptv.beta.spare.password", "secret-spare")
    hub.db.kv_set("iptv.logins", [
        {"provider": "thomas-creek", "login": "main", "server": "http://tc.test", "username": "main",
         "password_secret": "iptv.tc.main.password"},
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare",
         "password_secret": "iptv.beta.spare.password"},
    ])
    note_usage(hub, "thomas-creek", "main", {"max_connections": "1", "active_cons": "0", "status": "Active"})
    note_usage(hub, "beta", "spare", {"max_connections": beta_slots, "active_cons": "0", "status": "Active"})
