"""The phone's "My playlists" sheet: each provider's state, expiry and connections, with no address or login."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from hub import iptvaddr
from hub.iptvplay import note_usage


def _stamp(days: int) -> str:
    return str(int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp()))


def _setup(hub):
    hub.config.iptv.providers = []
    hub.vault.set("iptv.alpha.house.password", "secret-house")
    hub.vault.set("iptv.beta.spare.password", "secret-spare")
    hub.vault.set("iptv.gamma.old.password", "secret-old")
    hub.db.kv_set("iptv.provider_names", {"alpha": "Diamond", "beta": "Dream", "gamma": "Eagle"})
    hub.db.kv_set("iptv.logins", [
        {"provider": "alpha", "login": "house", "server": "http://alpha.test", "username": "house-user",
         "password_secret": "iptv.alpha.house.password"},
        {"provider": "beta", "login": "spare", "server": "http://beta.test", "username": "spare-user",
         "password_secret": "iptv.beta.spare.password"},
        {"provider": "gamma", "login": "old", "server": "http://gamma.test", "username": "old-user",
         "password_secret": "iptv.gamma.old.password"},
    ])
    note_usage(hub, "alpha", "house", {"max_connections": "2", "active_cons": "1", "exp_date": _stamp(90),
                                       "status": "Active"})
    note_usage(hub, "beta", "spare", {"max_connections": "1", "active_cons": "0", "exp_date": _stamp(3),
                                      "status": "Active"})
    note_usage(hub, "gamma", "old", {"max_connections": "1", "active_cons": "0", "exp_date": _stamp(-2),
                                     "status": "Active"})
    iptvaddr.set_backups(hub.db, "alpha", ["http://alpha-backup.test"], "http://alpha.test")
    iptvaddr.note(hub.db, "alpha", "http://alpha.test", False, "timed out", now=time.time())


def test_each_provider_says_how_it_is_doing_without_addresses(hub, people):
    _setup(hub)
    dylan = people["phones"]["Dylan"]
    got = dylan.client.get("/v1/tv/phone/providers", headers=dylan.h)
    assert got.status_code == 200, got.text
    body = got.json()
    rows = {p["name"]: p for p in body["providers"]}
    assert rows["Diamond"]["state"] == "ok"
    assert rows["Diamond"]["message"] == "Working on a backup address"
    assert rows["Diamond"]["connections"] == {"used": 1, "max": 2}
    assert rows["Diamond"]["addresses"]["total"] == 2 and rows["Diamond"]["addresses"]["using_backup"]
    assert rows["Dream"]["state"] == "expiring" and rows["Dream"]["message"].startswith("Expires in")
    assert rows["Eagle"]["state"] == "expired"
    assert body["all"]["name"] == "All playlists" and body["all"]["working"] == 2
    assert body["all"]["message"] == "2 working · 1 not working"
    assert body["all"]["connections"] == {"used": 1, "max": 4}
    text = got.text
    for secret in ("alpha.test", "alpha-backup", "house-user", "secret-house", "spare-user", '"house"'):
        assert secret not in text


def test_providers_need_a_signed_in_device(hub, people, away_client):
    assert away_client.get("/v1/tv/phone/providers").status_code == 401
