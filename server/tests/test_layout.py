"""Each person's Home arranged by the house brain (hub/brain/layout.py): the model picks, the guard rails decide."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from hub.brain.layout import EXAMPLES, Planner, part_of_day
from tests.test_brain import FakeProvider


def plan_answer(sections: list[str], cards: list[str] | None = None, shown: int = 2, why: str = "Evening at home"):
    return {"sections": sections, "cards": cards or [], "cards_shown": shown, "why": why, "unsure": False}


@pytest.fixture
def layout(hub, monkeypatch):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    monkeypatch.setattr(hub.policy, "now", lambda: datetime(2026, 11, 14, 18, 30, tzinfo=hub.policy.tz))
    p: Planner = hub.brain.layout
    every = {n: {"enabled": True, "ok": True, "title": t} for n, t in (
        ("home_assistant", "Home Assistant"), ("frigate", "Cameras"), ("firewalla", "Network"), ("trek", "Trips"),
        ("family_board", "Family board"), ("mealie", "Meals"), ("show_player", "Holiday lights"))}
    p.services = every  # type: ignore[attr-defined]
    monkeypatch.setattr(p, "_services", lambda: {k: dict(v) for k, v in p.services.items()})  # type: ignore[attr-defined]

    def use(*answers: Any) -> FakeProvider:
        fake = FakeProvider(*answers)
        hub.brain.router.provider = fake
        return fake
    p.use = use  # type: ignore[attr-defined]
    return p


def subject(hub, person, at_home: bool = True):
    return hub.subject_for_person(hub.db.person(person["id"]), at_home=at_home)


def card(rule: str, priority: int = 50, title: str = "A card") -> dict[str, Any]:
    return {"id": f"{rule}:x", "rule": rule, "priority": priority, "title": title}


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def ids(plan, shown: bool = True) -> list[str]:
    return [s["id"] for s in plan["sections"] if s["show"] is shown]


# -- without the model ----------------------------------------------------------------------------------------------
def test_examples_only_use_real_ids():
    from hub.brain.layout import ADULT_SECTIONS, KID_SECTIONS

    for e in EXAMPLES:
        table = ADULT_SECTIONS if e["situation"]["home"] == "adult" else KID_SECTIONS
        assert set(e["plan"]["sections"]) <= set(table), e["name"]
        assert set(e["plan"]["cards"]) <= set(e["situation"]["cards"]), e["name"]


def test_part_of_day():
    assert [part_of_day(datetime(2026, 1, 1, h)) for h in (6, 10, 14, 19, 23, 2)] == \
        ["early", "morning", "afternoon", "evening", "night", "night"]


def test_no_key_gives_the_default_order(hub, layout, people):
    hub.vault.set("anthropic.api_key", "")
    fake = layout.use()
    plan, cards = layout.plan_for(subject(hub, people["avery"]), [card("dinner", 60), card("habit", 40)])
    assert plan["source"] == "default" and plan["home"] == "adult" and plan["cards_shown"] == 2
    assert ids(plan)[:3] == ["whos_home", "rightnow", "weather"]
    assert "systems" not in ids(plan) and "systems" not in ids(plan, False)  # an adult can't see Systems
    assert [c["rule"] for c in cards] == ["dinner", "habit"]
    assert fake.requests == []


def test_guests_and_tvs_get_no_plan(hub, layout):
    guest = hub.db.add_person("Gwen", "guest")
    plan, cards = layout.plan_for(hub.subject_for_person(guest), [card("x")])
    assert plan is None and cards == [card("x")]


def test_home_snapshot_and_smart_carry_the_plan(hub, layout, home_client, people):
    hub.vault.set("anthropic.api_key", "")
    snap = home_client.get("/v1/home", headers=people["phones"]["Avery"].h).json()
    assert snap["plan"]["source"] == "default" and snap["plan"]["sections"]
    smart = home_client.get("/v1/smart", headers=people["phones"]["Theo"].h).json()
    assert smart["plan"]["home"] == "kid"


# -- with the model -------------------------------------------------------------------------------------------------
async def test_the_model_arranges_home_and_it_is_cached(hub, layout, people):
    fake = layout.use(plan_answer(["rightnow", "playing", "favorites", "modes"], ["habit", "dinner"], 1))
    s = subject(hub, people["avery"])
    cards = [card("dinner", 60, "Tacos at 6:15"), card("habit", 40)]
    first, _ = layout.plan_for(s, cards)
    assert first["source"] == "default"  # the request never waits for the model
    await settle()
    assert len(fake.requests) == 1
    sent = json.loads(fake.requests[0]["prompt"])
    assert sent["now"]["part"] == "evening" and sent["now"]["where"] == "home"
    assert {c["key"] for c in sent["now"]["cards"]} == {"dinner", "habit"}
    assert sent["examples"] and all(e["situation"]["home"] == "adult" for e in sent["examples"])
    assert fake.requests[0]["model"] == "claude-haiku-5-5"

    plan, ordered = layout.plan_for(s, cards)
    assert plan["source"] == "ai" and plan["why"] == "Evening at home" and plan["generated_at"]
    assert ids(plan) == ["rightnow", "playing", "favorites", "modes"]
    assert "whos_home" in ids(plan, False)  # left out = tucked under "Show everything", never lost
    assert [c["rule"] for c in ordered] == ["habit", "dinner"] and plan["cards_shown"] == 1
    await settle()
    assert len(fake.requests) == 1  # same situation: no second call

    [run] = hub.db.all("SELECT * FROM ai_runs WHERE job='layout'")
    assert run["outcome"] == "planned" and run["cost_usd"] > 0 and run["model"] == "claude-haiku-5-5"
    assert hub.brain.summary()["layout"]["runs"] == 1


async def test_a_new_situation_asks_again_after_the_cooldown(hub, layout, people, monkeypatch):
    fake = layout.use(plan_answer(["rightnow", "status", "favorites"]), plan_answer(["status", "rightnow", "modes"]))
    s = subject(hub, people["avery"])
    layout.plan_for(s, [])
    await settle()
    away = subject(hub, people["avery"], at_home=False)
    layout.plan_for(away, [])
    await settle()
    assert len(fake.requests) == 1  # cooldown
    layout._last_call.clear()
    layout.plan_for(away, [])
    await settle()
    assert len(fake.requests) == 2 and json.loads(fake.requests[1]["prompt"])["now"]["where"] == "away"
    assert ids(layout.plan_for(away, [])[0])[:2] == ["status", "rightnow"]


async def test_guard_rails(hub, layout, people):
    # Unknown ids, a section Avery can't have, and Right now left out with a safety card waiting.
    layout.use(plan_answer(["systems", "bogus", "favorites", "modes", "playing"], ["dinner", "oven_on"], 1))
    s = subject(hub, people["avery"])
    hub.settings.patch(s, {"home.hidden": ["modes"]}, None)
    cards = [card("dinner", 60), card("oven_on", 95)]
    layout.plan_for(s, cards)
    await settle()
    plan, ordered = layout.plan_for(s, cards)
    assert ids(plan) == ["rightnow", "favorites", "playing"]  # safety pulls Right now to the top
    assert "modes" not in ids(plan) + ids(plan, False)  # hidden by Avery stays hidden
    assert "systems" not in ids(plan) + ids(plan, False) and "bogus" not in ids(plan) + ids(plan, False)
    assert ordered[0]["rule"] == "oven_on" and plan["cards_shown"] >= 1


async def test_a_service_down_becomes_a_note_for_grown_ups_and_goes_away_for_kids(hub, layout, people):
    hub.vault.set("anthropic.api_key", "")
    layout.services["family_board"]["ok"] = False  # type: ignore[attr-defined]
    plan, _ = layout.plan_for(subject(hub, people["avery"]), [])
    today = next(x for x in plan["sections"] if x["id"] == "today")
    assert today["note"] == "Family board isn't answering right now"
    kid, _ = layout.plan_for(subject(hub, people["theo"]), [])
    assert "board" not in ids(kid) + ids(kid, False) and "todo" not in ids(kid) + ids(kid, False)
    # A service that isn't set up at all: no section, no note.
    layout.services.pop("frigate")  # type: ignore[attr-defined]
    plan, _ = layout.plan_for(subject(hub, people["dylan"]), [])
    assert "cameras" not in ids(plan) + ids(plan, False)


async def test_a_childs_request_is_about_the_child(hub, layout, people):
    fake = layout.use(plan_answer(["rightnow", "board", "todo", "actions", "cameras"], ["my_chores"], 1))
    s = subject(hub, people["theo"])
    layout.plan_for(s, [card("my_chores", 80, "Avery says clean up")])
    await settle()
    sent = json.loads(fake.requests[0]["prompt"])
    assert "where" not in sent["now"] and sent["now"]["home"] == "kid"
    assert "Avery" not in fake.requests[0]["prompt"] and "title" not in sent["now"]["cards"][0]
    assert all(e["situation"]["home"] == "kid" for e in sent["examples"])
    plan, _ = layout.plan_for(s, [card("my_chores", 80)])
    assert ids(plan) == ["rightnow", "board", "todo", "actions"]  # no cameras for a child


async def test_turning_it_off_for_yourself(hub, layout, people):
    fake = layout.use(plan_answer(["status", "rightnow", "modes"]))
    s = subject(hub, people["avery"])
    hub.settings.patch(s, {"home.ai": False, "home.order": ["favorites", "weather"]}, None)
    plan, _ = layout.plan_for(s, [])
    await settle()
    assert plan["source"] == "off" and ids(plan)[:2] == ["favorites", "weather"] and fake.requests == []


async def test_paused_or_switched_off_brain_uses_the_default(hub, layout, people):
    fake = layout.use(plan_answer(["status", "rightnow", "modes"]))
    hub.brain.set_paused(True)
    layout.plan_for(subject(hub, people["avery"]), [])
    hub.brain.set_paused(False)
    hub.config.brain.layout_enabled = False
    layout.plan_for(subject(hub, people["dylan"]), [])
    await settle()
    assert fake.requests == []


async def test_a_bad_answer_falls_back(hub, layout, people):
    fake = layout.use("not json at all")
    s = subject(hub, people["avery"])
    layout.plan_for(s, [])
    await settle()
    assert fake.requests and layout.plan_for(s, [])[0]["source"] == "default"


def test_weather_is_a_home_section_setting(home_client, people):
    r = home_client.patch("/v1/me/settings", json={"changes": {"home.order": ["weather", "rightnow"], "home.ai": False}},
                          headers=people["phones"]["Avery"].h)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["home.order"] == ["weather", "rightnow"] and r.json()["settings"]["home.ai"] is False
