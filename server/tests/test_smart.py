"""Smart "Right now" cards: rules, buttons that respect roles, Not now / mute, habits, weather."""

from __future__ import annotations

import time
from datetime import datetime

from hub.smart import Smart, hm
from hub.weather import shape

UPPER = "water_heater.ntz100487q_oven"


def at(hub, y=2026, mo=11, d=12, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=hub.policy.tz)


def subj(hub, person, phone):
    dev = hub.db.device(phone.device_id)
    return hub.subject_for(dev, True)


def rules(cards):
    return [c["rule"] for c in cards]


def test_lockup_card_offers_goodnight_and_it_runs(hub, people, home_client):
    hub.config.hub.dry_run = False
    hub.home.states["lock.front_door"]["state"] = "unlocked"
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    cards = hub.smart.cards(dylan, at(hub, h=22, mi=5))
    lock = next(c for c in cards if c["rule"] == "lockup")
    assert lock["title"] == "Lock up for the night"
    titles = {i["title"] for i in lock["items"]}
    assert "Front door" in titles and "Lights" in titles
    good = lock["buttons"][0]
    assert good["action"] == "steps.run"  # locking and lights off are everyday; unlocking would need Face ID
    # Lock-up beats everything else at night.
    assert cards[0]["rule"] == "lockup"

    ph = people["phones"]["Dylan"]
    r = ph.secure_post("/v1/actions", {"action": good["action"], "target": None, "params": good["params"]})
    assert r.status_code == 200, r.text
    assert ("lock", "lock", {"entity_id": "lock.front_door"}) in hub.calls
    assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in hub.calls


def test_children_never_get_house_cards_or_buttons(hub, people):
    hub.home.states["lock.front_door"]["state"] = "unlocked"
    theo = subj(hub, people["theo"], people["phones"]["Theo"])
    cards = hub.smart.cards(theo, at(hub, h=22, mi=5))
    assert "lockup" not in rules(cards)
    for c in cards:
        for b in c["buttons"]:
            assert b.get("action") not in ("lock.set", "steps.run", "garage.set", "oven.set")


def test_steps_run_refuses_unsafe_steps(hub, people, home_client):
    ph = people["phones"]["Dylan"]
    r = home_client.post("/v1/actions", json={"action": "steps.run", "params": {"steps": [
        {"action": "network.pause", "target": people["theo"]["id"], "params": {}}]}}, headers=ph.h)
    assert r.status_code == 400
    theo = people["phones"]["Theo"]
    r = home_client.post("/v1/actions", json={"action": "steps.run", "params": {"steps": [
        {"action": "light.set", "target": "light.kitchen", "params": {"on": False}}]}}, headers=theo.h)
    assert r.status_code == 403


def test_kid_bedtime_card_and_parent_pause(hub, people):
    hub.smart.internet = {people["theo"]["id"]: {"managed": True, "paused": False}}
    hub.config.smart.bedtimes = {"Theo": "20:30"}
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    cards = hub.smart.cards(dylan, at(hub, h=20, mi=10))
    bed = next(c for c in cards if c["rule"] == "bedtime")
    assert bed["title"] == "Theo's bedtime in 20 min"
    b = bed["buttons"][0]
    assert b["action"] == "network.pause" and b["params"]["minutes"] == (7 * 60 - (20 * 60 + 10)) % 1440
    theo = subj(hub, people["theo"], people["phones"]["Theo"])
    kid = hub.smart.cards(theo, at(hub, h=20, mi=10))
    assert any(c["rule"] == "my_bedtime" and c["title"] == "Bedtime in 20 min" for c in kid)


def test_dinner_card_preheats_from_the_recipe(hub, people):
    from tests.test_kitchen import oven

    hub.home.states[UPPER] = oven(UPPER)
    hub.home.dinner = {"title": "Sheet-pan chicken", "slug": "sheet-pan-chicken", "image_url": None}
    hub.smart.dinner_oven = {"slug": "sheet-pan-chicken",
                             "oven": {"mode": "Bake", "temp": 425, "minutes": 45, "title": "Sheet-pan chicken"}}
    hub.config.smart.dinner_time = "18:15"
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    early = next(c for c in hub.smart.cards(dylan, at(hub, h=17, mi=0)) if c["rule"] == "dinner")
    assert "Start the oven by 5:15 pm" in early["detail"]
    pre = early["buttons"][0]
    assert pre["action"] == "oven.set" and pre["target"] == UPPER and pre["secure"] is True
    assert pre["params"]["temp"] == 425 and pre["params"]["timer_min"] == 45
    late = next(c for c in hub.smart.cards(dylan, at(hub, h=17, mi=30)) if c["rule"] == "dinner")
    assert late["eyebrow"] == "Preheat now" and late["priority"] > early["priority"]
    # Outside the window there is no dinner card.
    assert "dinner" not in rules(hub.smart.cards(dylan, at(hub, h=13)))


def test_not_now_hides_until_tomorrow_and_three_strikes_mute(hub, people):
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    now = at(hub, h=20)
    hub.config.smart.trash_night = "thursday"  # 2026-11-12 is a Thursday
    c = next(x for x in hub.smart.cards(dylan, now) if x["rule"] == "trash")
    r = hub.smart.dismiss(dylan, c["id"], "trash", handled=False, now=now)
    assert r["muted"] is False
    assert "trash" not in rules(hub.smart.cards(dylan, now))
    nxt = at(hub, d=19, h=20)
    assert "trash" in rules(hub.smart.cards(dylan, nxt))
    hub.smart.dismiss(dylan, "trash:a", "trash", handled=False, now=nxt)
    r = hub.smart.dismiss(dylan, "trash:b", "trash", handled=False, now=nxt)
    assert r["muted"] is True
    assert "trash" not in rules(hub.smart.cards(dylan, at(hub, d=26, h=20)))
    hub.smart.set_muted(dylan, "trash", False)
    assert "trash" in rules(hub.smart.cards(dylan, at(hub, d=26, h=20)))


def test_away_cards(hub, people):
    hub.home.states["person.dylan"] = {"entity_id": "person.dylan", "state": "not_home", "attributes": {}}
    hub.home.states["lock.front_door"]["state"] = "unlocked"
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    cards = hub.smart.cards(dylan, at(hub, h=11))
    assert cards[0]["rule"] == "away_unlocked" and cards[0]["buttons"][0]["action"] == "lock.set"
    lights = next(c for c in cards if c["rule"] == "away_lights")
    assert lights["title"] == "1 light is on"


def test_habit_is_learned_and_offered(hub, people):
    pid = people["dylan"]["id"]
    tz = hub.policy.tz
    now = at(hub, h=17, mi=40)
    for back in range(1, 15):
        day = datetime.fromtimestamp(now.timestamp() - back * 86400, tz)
        if day.weekday() >= 5:
            continue
        ts = day.replace(hour=17, minute=45).timestamp()
        hub.db.log("light.set", "ok", person_id=pid, target="light.living_room", params={"on": True, "brightness": 80})
        hub.db.execute("UPDATE activity SET ts=? WHERE id=(SELECT max(id) FROM activity)", [ts])
    hub.smart.learn_habits(now.timestamp())
    assert hub.smart.habits[pid][0]["minute"] == 17 * 60 + 45
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    h = next(c for c in hub.smart.cards(dylan, now) if c["rule"] == "habit")
    assert h["title"] == "Living room to 80%"
    assert h["buttons"][0]["action"] == "light.set"
    assert "habit" not in rules(hub.smart.cards(dylan, at(hub, h=12)))


def test_home_snapshot_carries_cards_and_weather(hub, people, home_client):
    hub.smart.weather.data = {"temp": 58.4, "condition": "Clear", "symbol": "sun.max.fill", "high": 64, "low": 41}
    r = home_client.get("/v1/home", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200
    body = r.json()
    assert body["weather"]["temp"] == 58.4
    assert isinstance(body["smart"], list)
    r = home_client.get("/v1/smart", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200 and "cards" in r.json()
    r = home_client.post("/v1/smart/dismiss", json={"id": "x", "rule": "trash"}, headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200
    r = home_client.get("/v1/smart/settings", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200 and r.json()["rules"]


def test_weather_shape_finds_rain_and_tonights_low():
    raw = {
        "current": {"time": "2026-11-12T13:00", "temperature_2m": 58.0, "weather_code": 2, "is_day": 1},
        "hourly": {"time": [f"2026-11-12T{h:02d}:00" for h in range(24)] + [f"2026-11-13T{h:02d}:00" for h in range(24)],
                   "temperature_2m": [50] * 13 + [55, 54, 52, 48, 45, 40, 38, 36, 34, 33, 31] + [30] * 9 + [40] * 15,
                   "precipitation_probability": [0] * 15 + [70] + [10] * 32,
                   "weather_code": [2] * 15 + [63] + [2] * 32},
        "daily": {"temperature_2m_max": [60, 50], "temperature_2m_min": [40, 29], "sunrise": ["2026-11-12T06:52", "x"],
                  "sunset": ["2026-11-12T17:12", "x"], "precipitation_probability_max": [70, 10]},
    }
    w = shape(raw)
    assert w["condition"] == "Partly cloudy" and w["rain_at"] == "2026-11-12T15:00"
    assert w["tonight_low"] == 30 and w["sunset"] == "2026-11-12T17:12"


def test_hm():
    assert hm("18:15", 0) == 1095 and hm("bad", 7) == 7
