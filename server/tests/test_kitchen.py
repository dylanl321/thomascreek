"""Ovens (GE via Home Assistant), recipe reading, recipe library and dinner plan."""

from __future__ import annotations

import asyncio
import time
from datetime import date

import pytest

from hub.kitchen import oven_plan, parse_recipe_text

UPPER = "water_heater.ntz100487q_oven"
LOWER = "water_heater.ntz100487q_lower_oven"


def oven(eid, state="off", remote=True, current=75, target=0, name=None):
    return {"entity_id": eid, "state": state, "last_updated": "2026-11-12T01:00:00+00:00",
            "last_changed": "2026-11-12T01:00:00+00:00",
            "attributes": {"friendly_name": name or ("NTZ100487Q Lower Oven" if "lower" in eid else "NTZ100487Q Oven"),
                           "operation_list": ["off", "Bake", "Convection Bake", "Convection Roast", "Broil High", "Warm"],
                           "operation_mode": state, "current_temperature": current, "temperature": target,
                           "min_temp": 170, "max_temp": 550, "supported_features": 3 if remote else 0,
                           "icon": "mdi:stove"}}


@pytest.fixture
def ovens(hub):
    hub.config.hub.dry_run = False
    hub.home.states[UPPER] = oven(UPPER)
    hub.home.states[LOWER] = oven(LOWER)
    hub.home.states["water_heater.steam_shower"] = {"entity_id": "water_heater.steam_shower", "state": "off",
                                                    "attributes": {"friendly_name": "Steam shower"}}
    sent = []

    async def send(**kw):
        sent.append(kw)
        return []

    hub.notify.send = send  # type: ignore[method-assign]
    hub.sent = sent  # type: ignore[attr-defined]
    return hub


def _post(client, ph, action, target, params=None):
    return client.post("/v1/actions", json={"action": action, "target": target, "params": params or {}}, headers=ph.h)


def test_ovens_are_their_own_kind_and_labelled(ovens, people, home_client):
    from hub.home import kind_for

    assert kind_for(ovens.home.states[UPPER]) == "oven"
    assert kind_for(ovens.home.states["water_heater.steam_shower"]) == "shower"
    dylan = people["phones"]["Dylan"]
    r = home_client.get("/v1/kitchen", headers=dylan.h)
    assert r.status_code == 200, r.text
    names = {o["id"]: o["name"] for o in r.json()["ovens"]}
    assert names == {UPPER: "Upper oven", LOWER: "Lower oven"}
    assert r.json()["ovens"][0]["remote_ready"] is True
    theo = people["phones"]["Theo"]
    assert home_client.get("/v1/kitchen", headers=theo.h).status_code == 403


def test_starting_an_oven_needs_face_id_and_sets_mode_then_temp(ovens, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = _post(home_client, dylan, "oven.set", UPPER, {"mode": "Convection Roast", "temp": 425})
    assert r.status_code == 428
    r = dylan.secure_post("/v1/actions", {"action": "oven.set", "target": UPPER,
                                          "params": {"mode": "Convection Roast", "temp": 425, "recipe": "Chicken"}})
    assert r.status_code == 200, r.text
    assert ovens.calls == [
        ("water_heater", "set_operation_mode", {"entity_id": UPPER, "operation_mode": "Convection Roast"}),
        ("water_heater", "set_temperature", {"entity_id": UPPER, "temperature": 425}),
    ]
    assert "Upper oven" in r.json()["message"]
    assert ovens.kitchen.sessions[UPPER].title == "Chicken"


def test_turning_off_is_everyday(ovens, people, home_client):
    avery = people["phones"]["Avery"]
    r = _post(home_client, avery, "oven.set", LOWER, {"on": False})
    assert r.status_code == 200, r.text
    assert ovens.calls[-1] == ("water_heater", "set_operation_mode", {"entity_id": LOWER, "operation_mode": "off"})


def test_children_cannot_touch_ovens(ovens, people, home_client):
    theo = people["phones"]["Theo"]
    assert _post(home_client, theo, "oven.set", UPPER, {"on": False}).status_code == 403


def test_remote_enable_and_offline_are_explained(ovens, people, home_client):
    dylan = people["phones"]["Dylan"]
    ovens.home.states[UPPER] = oven(UPPER, remote=False)
    r = _post(home_client, dylan, "oven.set", UPPER, {"mode": "Bake", "temp": 350})
    assert r.status_code == 409 and r.json()["error"] == "remote_enable"
    ovens.home.states[UPPER] = oven(UPPER, state="unavailable")
    r = _post(home_client, dylan, "oven.set", UPPER, {"mode": "Bake", "temp": 350})
    assert r.status_code == 409 and r.json()["error"] == "oven_offline"
    assert ovens.calls == []


def test_unknown_mode_and_temp_clamp(ovens, people, home_client):
    dylan = people["phones"]["Dylan"]
    assert dylan.secure_post("/v1/actions", {"action": "oven.set", "target": UPPER,
                                             "params": {"mode": "Self Clean"}}).status_code == 400
    r = dylan.secure_post("/v1/actions", {"action": "oven.set", "target": UPPER, "params": {"mode": "Bake", "temp": 900}})
    assert r.status_code == 200
    assert ovens.calls[-1][2]["temperature"] == 550


def test_preheat_notice_then_timer_and_auto_off(ovens, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "oven.set", "target": UPPER, "params": {
        "mode": "Bake", "temp": 400, "timer_min": 15, "timer_from": "manual", "auto_off": True, "recipe": "Salmon"}})
    assert r.status_code == 200
    ovens.home.states[UPPER] = oven(UPPER, state="Bake", current=398, target=400)
    asyncio.run(ovens.kitchen.tick())
    note = ovens.sent[-1]
    assert note["title"] == "Upper oven is at 400°" and note["cat"] == "kitchen"
    assert [a["id"] for a in note["actions"]] == ["timer", "off"]
    # Tapping "Start timer" on the notice.
    r = _post(home_client, dylan, "oven.timer", UPPER, {"minutes": 15})
    assert r.status_code == 200, r.text
    pending = ovens.timers.pending("oven.timer")
    assert len(pending) == 1 and abs(pending[0]["due_at"] - (time.time() + 900)) < 5
    asyncio.run(ovens.timers.tick(now=time.time() + 901))
    assert ovens.calls[-1] == ("water_heater", "set_operation_mode", {"entity_id": UPPER, "operation_mode": "off"})
    assert ovens.sent[-1]["title"] == "Time's up: Salmon"
    assert "turned off" in ovens.sent[-1]["body"]


def test_still_on_reminder(ovens):
    ovens.home.states[LOWER] = oven(LOWER, state="Bake", current=350, target=350)
    asyncio.run(ovens.kitchen.tick(now=time.time() + 4 * 3600 + 3e9))  # long after last_changed
    assert ovens.sent and ovens.sent[-1]["title"] == "Lower oven is still on"
    n = len(ovens.sent)
    asyncio.run(ovens.kitchen.tick(now=time.time() + 4 * 3600 + 3e9))
    assert len(ovens.sent) == n  # once


def test_reading_recipes():
    p = oven_plan(["Preheat the oven to 425°F.", "Toss.", "Roast for 20-25 minutes, until golden."])
    assert p["mode"] == "Bake" and p["temp"] == 425 and p["minutes"] == 25
    p = oven_plan(["Heat a convection oven to 200C.", "Roast 1 hour 10 minutes"])
    assert p["mode"] == "Convection Roast" and p["temp"] == 390 and p["minutes"] == 70
    assert oven_plan(["Boil the pasta for 10 minutes."]) is None
    p = oven_plan(["Turn on the broiler.", "Broil 3 to 4 minutes."])
    assert p["mode"] == "Broil High" and p["temp"] is None and p["minutes"] == 4
    p = oven_plan(["Preheat oven to 350 degrees.", "Mix."], "PT40M")
    assert p["minutes"] == 40
    d = parse_recipe_text("Salmon\nServes 4\nIngredients\n1 lb salmon\n- 2 tbsp honey\nDirections\n"
                          "1. Preheat oven to 400°F. Line a\npan with foil.\n2. Bake 12 to 15 minutes.")
    assert d["title"] == "Salmon" and d["ingredients"] == ["1 lb salmon", "2 tbsp honey"]
    assert d["steps"] == ["Preheat oven to 400°F. Line a pan with foil.", "Bake 12 to 15 minutes."]
    assert d["oven"]["temp"] == 400 and d["oven"]["minutes"] == 15


class FakeMealie:
    enabled = True
    last_error = None

    async def close(self):
        pass

    def health(self):
        return {"id": "mealie", "ok": True}

    def __init__(self):
        self.recipes_db = {"salmon": {"id": "11111111-1111-1111-1111-111111111111", "slug": "salmon", "title": "Salmon",
                                      "tags": [], "favorite": False, "last_made": None, "ingredients": ["1 lb salmon"]},
                           "chili": {"id": "22222222-2222-2222-2222-222222222222", "slug": "chili", "title": "Chili",
                                     "tags": ["Favorite"], "favorite": True, "last_made": "2026-11-10T00:00:00",
                                     "ingredients": []}}
        self.planned: dict[str, str] = {}
        self.updates = []

    async def recipes(self, search="", tag=None, page=1, per_page=60):
        return {"items": list(self.recipes_db.values()), "total": len(self.recipes_db), "page": 1, "pages": 1}

    async def recipe(self, slug):
        return {**self.recipes_db[slug], "oven": None}

    async def create(self, name):
        self.recipes_db["new"] = {"id": "3" * 32, "slug": "new", "title": name, "tags": [], "favorite": False,
                                  "last_made": None, "ingredients": []}
        return "new"

    async def update(self, slug, fields):
        self.updates.append((slug, fields))
        return slug

    async def plan(self, start, days=7):
        return [{"date": d, "meal": "dinner", "slug": s, "title": s, "id": 1}
                for d, s in self.planned.items()]

    async def plan_set(self, day, recipe_id, title, meal="dinner"):
        slug = next((k for k, v in self.recipes_db.items() if v["id"] == recipe_id), title)
        self.planned[day.isoformat()] = slug


@pytest.fixture
def meals(hub):
    hub.config.hub.dry_run = False
    hub.mealie = FakeMealie()
    return hub


def test_add_and_edit_recipe_needs_family_edit(meals, people, home_client):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    body = {"title": "Pesto pasta", "ingredients": ["1 lb pasta"], "steps": ["Boil."],
            "oven": {"mode": "Bake", "temp": 375, "minutes": 20}}
    assert home_client.post("/v1/meals/recipes", json=body, headers=theo.h).status_code == 403
    r = home_client.post("/v1/meals/recipes", json=body, headers=dylan.h)
    assert r.status_code == 200, r.text
    slug, fields = meals.mealie.updates[-1]
    assert fields["oven"] == {"mode": "Bake", "temp": 375, "minutes": 20} and fields["title"] == "Pesto pasta"
    r = home_client.put("/v1/meals/recipes/new", json={"no_oven": True}, headers=dylan.h)
    assert r.status_code == 200 and meals.mealie.updates[-1][1] == {"oven": None}
    assert home_client.post("/v1/meals/recipes", json={"url": "not a link"}, headers=dylan.h).status_code == 400
    assert home_client.put("/v1/meals/recipes/..%2Fetc", json={}, headers=dylan.h).status_code in (404, 405)


def test_plan_week_and_fill(meals, people, home_client):
    dylan = people["phones"]["Dylan"]
    today = meals.policy.now().date().isoformat()
    r = home_client.put(f"/v1/meals/plan/{today}", json={"slug": "salmon"}, headers=dylan.h)
    assert r.status_code == 200, r.text
    r = home_client.get("/v1/meals/plan", headers=dylan.h)
    days = r.json()["days"]
    assert len(days) == 7 and days[0]["dinner"]["slug"] == "salmon"
    r = home_client.post("/v1/meals/plan/fill", json={}, headers=dylan.h)
    assert r.status_code == 200, r.text
    assert r.json()["filled"] == 1  # only chili is left to suggest
    assert set(meals.mealie.planned.values()) == {"salmon", "chili"}
    r = home_client.post("/v1/meals/recipes/parse", json={"text": "Toast\nIngredients\n1 slice bread"}, headers=dylan.h)
    assert r.json()["title"] == "Toast"
    assert date.fromisoformat(today)
