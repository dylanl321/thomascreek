"""Who's home from Wi-Fi, the phone's circle round the house and Home Assistant (hub/presence.py)."""

from __future__ import annotations

import asyncio
import time

from hub.presence import LEAVE_LAG_S, LOCATION_TTL_S, WIFI_GRACE_S, combine

NOW = 1_800_000_000.0
PHONE_MAC = "AA:BB:CC:00:00:01"


def dev(online: bool, last_seen: float, since: float | None = None) -> dict:
    return {"online": online, "last_seen": last_seen, "online_since": since if online else None}


# -- the combining rule -----------------------------------------------------------

def test_nothing_known_is_unknown():
    assert combine(ha=None, location=None, wifi=[], now=NOW)["home"] is None


def test_wifi_online_is_home_and_grace_before_away():
    r = combine(ha=None, location=None, wifi=[dev(True, NOW, NOW - 3600)], now=NOW)
    assert r["home"] is True and r["source"] == "wifi" and r["changed_at"] == NOW - 3600
    # Off Wi-Fi a few minutes (a sleeping phone): still home.
    assert combine(ha=None, location=None, wifi=[dev(False, NOW - 300)], now=NOW)["home"] is True
    # Off longer than the grace: away, from when it was last seen plus the grace.
    r = combine(ha=None, location=None, wifi=[dev(False, NOW - WIFI_GRACE_S - 60)], now=NOW)
    assert r["home"] is False and r["changed_at"] == NOW - 60


def test_circle_owns_leaving():
    # Arrived by the circle, phone asleep off Wi-Fi for an hour: still home.
    r = combine(ha=None, location={"home": True, "at": NOW - 7200}, wifi=[dev(False, NOW - 3600)], now=NOW)
    assert r["home"] is True and r["source"] == "phone"
    # Just left by the circle; the Firewalla still lists the phone for a few minutes: away.
    r = combine(ha=None, location={"home": False, "at": NOW - 120}, wifi=[dev(True, NOW, NOW - 7200)], now=NOW)
    assert r["home"] is False and r["source"] == "phone"
    # Left, then back on home Wi-Fi well after (the arrive was missed): home.
    leave = NOW - LEAVE_LAG_S - 600
    r = combine(ha=None, location={"home": False, "at": leave}, wifi=[dev(True, NOW, NOW - 60)], now=NOW)
    assert r["home"] is True and r["source"] == "wifi"


def test_old_circle_report_lapses_to_wifi():
    r = combine(ha=None, location={"home": True, "at": NOW - LOCATION_TTL_S - 1},
                wifi=[dev(False, NOW - 2 * WIFI_GRACE_S)], now=NOW)
    assert r["home"] is False and r["source"] == "wifi"


def test_latest_news_wins_over_home_assistant():
    ha = {"home": False, "changed_at": NOW - 7200, "where": "Work"}
    r = combine(ha=ha, location=None, wifi=[dev(True, NOW, NOW - 600)], now=NOW)
    assert r["home"] is True and r["source"] == "wifi" and r["where"] is None
    r = combine(ha={"home": False, "changed_at": NOW - 60, "where": "Work"}, location=None,
                wifi=[dev(False, NOW - 3 * WIFI_GRACE_S)], now=NOW)
    assert r["home"] is False and r["source"] == "home_assistant" and r["where"] == "Work"


# -- the hub learns whose phone is whose, then reads the Firewalla -----------------

class FakeFirewalla:
    enabled = True

    def __init__(self, rows):
        self.rows = rows

    async def devices(self):
        return self.rows


def test_phone_learned_from_home_wifi_then_seen_by_firewalla(hub, home_client, people, monkeypatch):
    fw = FakeFirewalla([{"mac": PHONE_MAC, "ip": "192.168.1.50", "online": True, "last_seen": str(time.time())}])
    monkeypatch.setattr(hub, "firewalla", fw)
    assert asyncio.run(hub.household.wifi.poll()) is True
    ph = people["phones"]["Avery"]
    # Not learned yet: the hub can't tell.
    assert hub.household.wifi.for_person(people["avery"]) == []
    # Avery's app calls from home Wi-Fi (the test client's address): the hub learns the device.
    home_client.get("/v1/family/presence", headers=ph.h)
    fam = {p["first"]: p for p in home_client.get("/v1/family/presence", headers=ph.h).json()["people"]}
    assert fam["Avery"]["home"] is True and fam["Avery"]["source"] == "wifi"
    # Every test phone shares one address, so the last caller holds the device: Avery's.
    assert fam["Dylan"]["home"] is None
    # The address is stored on the hub, never sent to the app.
    assert PHONE_MAC not in home_client.get("/v1/family/presence", headers=ph.h).text
    # The phone goes quiet for longer than the grace: away.
    hub.household.wifi.update([{"mac": PHONE_MAC, "ip": None, "online": False,
                                "last_seen": time.time() - WIFI_GRACE_S - 120}], time.time())
    assert hub.household.presence(people["avery"])["home"] is False


def test_learned_address_moves_with_the_phone(hub, people):
    w = hub.household.wifi
    w.update([{"mac": PHONE_MAC, "ip": "192.168.1.77", "online": True}], NOW)
    assert w.saw_at_home(people["dylan"]["id"], "admin", "dev-d", "192.168.1.77", now=NOW)
    # The same address now answers for Avery's phone (a rotated private address reused): it moves.
    assert w.saw_at_home(people["avery"]["id"], "adult", "dev-a", "192.168.1.77", now=NOW + 10)
    assert w.macs_for(people["dylan"]) == set() and w.macs_for(people["avery"]) == {PHONE_MAC}
    # Dylan's phone comes back with a new private address: learned again.
    w.update([{"mac": "AA:BB:CC:00:00:02", "ip": "192.168.1.80", "online": True}], NOW + 20)
    assert w.saw_at_home(people["dylan"]["id"], "admin", "dev-d", "192.168.1.80", now=NOW + 20)
    assert w.macs_for(people["dylan"]) == {"AA:BB:CC:00:00:02"}
    # A shared tablet or a guest never claims a device.
    assert not w.saw_at_home("someone", "tablet", "dev-t", "192.168.1.80", now=NOW + 30)
    assert not w.saw_at_home("someone", "guest", "dev-g", "192.168.1.80", now=NOW + 30)


def test_child_device_by_firewalla_group(hub, people):
    hub.household.wifi.update([{"mac": "AA:BB:CC:00:00:09", "online": True, "group": "Theo"}], time.time())
    pr = hub.household.presence(people["theo"])
    assert pr["home"] is True and pr["source"] == "wifi"


def test_wifi_leaving_runs_the_last_one_out_check(hub, people):
    w = hub.household.wifi
    now = time.time()
    w.update([{"mac": PHONE_MAC, "ip": "192.168.1.77", "online": True}], now)
    w.saw_at_home(people["avery"]["id"], "adult", "dev-a", "192.168.1.77", now=now)
    assert asyncio.run(hub.household.tick()) == []
    w.update([{"mac": PHONE_MAC, "online": False, "last_seen": now - WIFI_GRACE_S - 60}], now)
    assert asyncio.run(hub.household.tick()) == [people["avery"]["id"]]


# -- the phone's circle: refreshes and off -----------------------------------------

def test_refresh_has_no_side_effects_and_off_forgets(hub, home_client, people):
    ph = people["phones"]["Avery"]
    r = home_client.post("/v1/presence", json={"event": "inside"}, headers=ph.h)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert hub.household.presence(people["avery"])["home"] is True
    first_at = hub.db.kv_get(f"presence.{people['avery']['id']}")["at"]
    home_client.post("/v1/presence", json={"event": "inside"}, headers=ph.h)
    assert hub.db.kv_get(f"presence.{people['avery']['id']}")["at"] == first_at  # keeps the real crossing time
    r = home_client.post("/v1/presence", json={"event": "outside"}, headers=ph.h).json()
    assert r == {"ok": True}  # no departure check from a refresh
    assert hub.household.presence(people["avery"])["home"] is False
    home_client.post("/v1/presence", json={"event": "off"}, headers=ph.h)
    assert hub.household.presence(people["avery"])["home"] is None
    assert home_client.post("/v1/presence", json={"event": "where"}, headers=ph.h).status_code == 422
