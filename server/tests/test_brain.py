"""The house brain: detection in Python, wording by a (fake) cheap model, cards on Home, a costed run log."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from hub.brain.jobs import Trigger
from hub.brain.models import Completion, cost


class FakeProvider:
    """Answers with scripted JSON (or a function of the request) and records every request."""

    def __init__(self, *answers: Any, tokens: tuple[int, int] = (1000, 200)):
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []
        self.tokens = tokens

    async def complete(self, **kw: Any) -> Completion:
        self.requests.append(kw)
        a = self.answers.pop(0) if self.answers else {"items": [], "unsure": False}
        if callable(a):
            a = a(kw)
        return Completion(text=json.dumps(a), model=kw["model"], tokens_in=self.tokens[0], tokens_out=self.tokens[1])

    def items(self, i: int = -1) -> list[dict[str, Any]]:
        return json.loads(self.requests[i]["prompt"])["items"]


def iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def brain(hub, monkeypatch):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    hub.config.brain.layout_enabled = False  # Home layout has its own tests (test_layout.py)
    b = hub.brain
    # An evening by default, so the daytime and morning jobs only speak when a test sets the clock.
    monkeypatch.setattr(b, "now", lambda: datetime(2026, 11, 14, 20, 0, tzinfo=hub.policy.tz))

    def use(*answers: Any, **kw: Any) -> FakeProvider:
        fake = FakeProvider(*answers, **kw)
        b.router.provider = fake
        return fake
    b.use = use  # type: ignore[attr-defined]
    return b


def word_all(title: str = "Washer's done", detail: str = "Move it to the dryer"):
    """A model that words every item it's given."""
    return lambda kw: {"items": [{"key": i["key"], "title": title, "detail": detail, "priority": 70}
                                 for i in json.loads(kw["prompt"])["items"]], "unsure": False}


def smart_cards(client, phone) -> list[dict[str, Any]]:
    r = client.get("/v1/smart", headers=phone.h)
    assert r.status_code == 200, r.text
    return [c for c in r.json()["cards"] if c["rule"].startswith("brain_")]


def runs(hub, job: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ai_runs" + (" WHERE job=?" if job else "") + " ORDER BY ts"
    return hub.db.all(sql, [job] if job else [])


def washer_cycle(hub):
    running = {"entity_id": "sensor.washer_status", "state": "Running",
               "attributes": {"friendly_name": "Washer status"}, "last_changed": iso_ago(3000)}
    done = {**running, "state": "Idle", "last_changed": iso_ago(5)}
    hub.home.states["sensor.washer_status"] = done
    hub.brain.on_state("sensor.washer_status", running, done)


# -- models ---------------------------------------------------------------------------------------------------------
def test_prices():
    assert cost("claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert cost("claude-haiku-5-5", 10_000, 1_000_000) == pytest.approx(0.501)
    assert cost("claude-haiku-5-5", 0, 0, cached=100_000) == pytest.approx(0.001)
    assert cost("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)  # past 100k the long-prompt price applies
    assert cost("claude-sonnet-5-5-20260601", 1_000_000, 0) == pytest.approx(2.0)
    assert cost("some-new-model", 1_000_000, 0) == pytest.approx(2.0)  # unknown: priced like Sonnet, never as free


async def test_unsure_cheap_answer_goes_to_the_smart_model(hub, brain):
    fake = brain.use({"items": [], "unsure": True}, {"items": [], "unsure": False})
    t = await brain.router.think(system="s", payload={"x": 1}, schema={})
    assert [r["model"] for r in fake.requests] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert t.escalated and t.tier == "smart" and t.model == "claude-sonnet-5-5"
    assert t.cost_usd == pytest.approx(cost("claude-haiku-5-5", 1000, 200) + cost("claude-sonnet-5-5", 1000, 200))
    assert t.tokens_in == 2000 and len(t.calls) == 2


def test_config_defaults(hub):
    cfg = hub.config.brain
    assert cfg.enabled and cfg.cheap_model == "claude-haiku-5-5" and cfg.smart_model == "claude-sonnet-5-5"
    assert cfg.monthly_cap_usd is None and cfg.auto_actions == []
    assert all(hub.brain.settings(j).daily_budget_usd is None for j in hub.brain.jobs.values())
    assert {j.id for j in hub.brain.jobs.values()} == {"plain_cards", "left_on", "health", "briefing", "catch_up"}


# -- 1. plain-language cards -----------------------------------------------------------------------------------------
async def test_washer_done_becomes_a_worded_card(hub, brain, home_client, people):
    fake = brain.use(word_all())
    washer_cycle(hub)
    assert "plain_cards" in brain._pending and brain._pending["plain_cards"][1].kind == "ha"
    assert await brain.run("plain_cards") == "suggested"
    sent = fake.items()
    assert sent[0]["kind"] == "appliance_done" and sent[0]["name"] == "Washer"
    assert "buttons" not in sent[0]  # delivery details stay on the hub
    cards = smart_cards(home_client, people["phones"]["Avery"])
    assert len(cards) == 1
    c = cards[0]
    assert c["title"] == "Washer's done" and c["detail"] == "Move it to the dryer" and c["rule"] == "brain_plain_cards"
    assert c["why"].startswith("House brain") and c["ai"]["job"] == "plain_cards"
    assert smart_cards(home_client, people["phones"]["Theo"]) == []  # grown-ups only
    [run] = runs(hub, "plain_cards")
    assert run["model"] == "claude-haiku-5-5" and run["tier"] == "cheap" and run["outcome"] == "suggested"
    assert run["cost_usd"] == pytest.approx(cost("claude-haiku-5-5", 1000, 200))
    assert run["tokens_in"] == 1000 and run["tokens_out"] == 200 and run["card_id"] == c["id"]
    assert run["trigger"] == "manual:run" and "Washer finished" in run["input_summary"]
    # Same situation again: no second model call, no second run
    assert await brain.run("plain_cards") == "unchanged"
    assert len(fake.requests) == 1 and len(runs(hub, "plain_cards")) == 1
    # "Not now" goes to the run log
    r = home_client.post("/v1/smart/dismiss", json={"id": c["id"], "rule": c["rule"], "handled": False},
                         headers=people["phones"]["Avery"].h)
    assert r.status_code == 200
    assert runs(hub, "plain_cards")[0]["feedback"] == "not_now"
    assert smart_cards(home_client, people["phones"]["Avery"]) == []
    assert smart_cards(home_client, people["phones"]["Dylan"])  # still there for the others


async def test_using_a_card_marks_it_used(hub, brain, home_client, people):
    brain.use(word_all())
    washer_cycle(hub)
    await brain.run("plain_cards")
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    home_client.post("/v1/smart/dismiss", json={"id": c["id"], "rule": c["rule"], "handled": True},
                     headers=people["phones"]["Dylan"].h)
    assert runs(hub, "plain_cards")[0]["feedback"] == "used"


async def test_without_a_key_the_template_words_it_for_free(hub, brain, home_client, people):
    fake = brain.use(word_all())
    hub.vault.delete("anthropic.api_key") if hasattr(hub.vault, "delete") else hub.vault.set("anthropic.api_key", "")
    washer_cycle(hub)
    assert await brain.run("plain_cards") == "suggested"
    assert fake.requests == []
    c = smart_cards(home_client, people["phones"]["Avery"])[0]
    assert c["title"] == "Washer finished"
    [run] = runs(hub, "plain_cards")
    assert run["model"] == "template" and run["cost_usd"] == 0 and run["decision"]["fallback"] == "no_key"


async def test_lights_on_all_day(hub, brain, home_client, people, monkeypatch):
    brain.use(word_all("Kitchen light on all day", ""))
    monkeypatch.setattr(brain, "now", lambda: datetime(2026, 11, 14, 13, 0, tzinfo=hub.policy.tz))
    hub.home.states["light.kitchen"]["last_changed"] = iso_ago(5 * 3600)
    assert await brain.run("plain_cards") == "suggested"
    c = smart_cards(home_client, people["phones"]["Avery"])[0]
    assert c["title"] == "Kitchen light on all day"
    off = next(b for b in c["buttons"] if b.get("action") == "steps.run")
    assert off["params"]["steps"] == [{"action": "light.set", "target": "light.kitchen", "params": {"on": False}}]


# -- 2. left on, left open ------------------------------------------------------------------------------------------
def empty_room(hub, light_age: float = 3600):
    hub.home.states["binary_sensor.living_motion"] = {
        "entity_id": "binary_sensor.living_motion", "state": "off",
        "attributes": {"friendly_name": "Living motion", "device_class": "motion"}, "last_changed": iso_ago(40 * 60)}
    hub.home.entity_area["binary_sensor.living_motion"] = "living_room"
    hub.home.states["light.living_room"].update(state="on", last_changed=iso_ago(light_age))


async def test_light_on_in_an_empty_room(hub, brain, home_client, people):
    brain.use(word_all("Living room's empty", "Lights still on"))
    empty_room(hub)
    assert await brain.run("left_on") == "suggested"
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    assert c["rule"] == "brain_left_on" and c["title"] == "Living room's empty"
    off = next(b for b in c["buttons"] if b.get("action") == "steps.run")
    assert off["params"]["steps"][0]["target"] == "light.living_room"
    assert hub.calls == []  # suggest only: nothing was switched off


async def test_a_light_someone_just_turned_on_is_left_alone(hub, brain, home_client, people):
    fake = brain.use(word_all())
    empty_room(hub, light_age=60)
    assert await brain.run("left_on") == "nothing"
    empty_room(hub)
    hub.db.log("light.set", "ok", target="light.living_room", person_id=people["avery"]["id"])
    assert await brain.run("left_on") == "nothing" and fake.requests == []


async def test_tv_left_on_when_the_last_person_leaves(hub, brain, home_client, people):
    brain.use(word_all("Theo's TV is still on", ""))
    hub.home.states["media_player.theo_tv"].update(state="playing", last_changed=iso_ago(3600))
    assert await brain.run("left_on", Trigger("hub", "last_left")) == "suggested"
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    step = next(b for b in c["buttons"] if b.get("action") == "steps.run")["params"]["steps"][0]
    assert step == {"action": "media.command", "target": "media_player.theo_tv", "params": {"command": "power", "on": False}}


# -- 3. health watch ------------------------------------------------------------------------------------------------
async def test_unavailable_devices_reach_the_admin_inbox_once(hub, brain, home_client, people):
    fake = brain.use({"title": "Kitchen light dropped off", "detail": "Check its Zigbee router.", "items": [],
                      "unsure": False})
    hub.home.states["light.kitchen"].update(state="unavailable", last_changed=iso_ago(3600))
    assert await brain.run("health") == "notified"
    inbox = hub.db.all("SELECT * FROM inbox WHERE group_key='brain.health'")
    assert [i["person_id"] for i in inbox] == [people["dylan"]["id"]]
    assert inbox[0]["level"] == "maintenance" and inbox[0]["title"] == "Kitchen light dropped off"
    assert fake.items()[0]["kind"] == "unavailable" and fake.items()[0]["count"] == 1
    assert await brain.run("health") == "unchanged"
    assert len(hub.db.all("SELECT * FROM inbox WHERE group_key='brain.health'")) == 1


async def test_ha_link_down_and_nas_alerts(hub, brain, people):
    brain.use({"title": "Home Assistant link is down", "detail": "", "items": [], "unsure": False})
    brain._ha_down_since = time.time() - 600
    hub.ha_ws.connected = False
    hub.home.systems = {"alerts": [{"level": "CRITICAL", "text": "Pool tank is DEGRADED"},
                                   {"level": "INFO", "text": "noise"}], "apps": {"crashed_names": ["plex"]}}
    assert await brain.run("health") == "notified"
    kinds = [i["kind"] for i in brain.router.provider.items()]
    assert "nas_alert" in kinds and "nas_apps" in kinds
    assert ("ha_link" in kinds) == hub.ha_ws.enabled


# -- 4. morning briefing ----------------------------------------------------------------------------------------------
async def test_morning_briefing(hub, brain, home_client, people, monkeypatch):
    brain.use({"title": "Rain after lunch", "detail": "Umbrellas for school.",
               "items": [{"title": "Weather", "value": "48°, rain at 1"}, {"title": "Tonight", "value": "Trash"}],
               "unsure": False})
    monkeypatch.setattr(brain, "now", lambda: datetime(2026, 11, 12, 7, 0, tzinfo=hub.policy.tz))  # a Thursday
    hub.smart.weather.data = {"temp": 48.2, "high": 55, "low": 40, "condition": "Cloudy", "rain_at": "2026-11-12T13:00"}
    assert await brain.run("briefing", Trigger("schedule", "at 07:00")) == "suggested"
    sent = {i["kind"] for i in brain.router.provider.items()}
    assert {"weather", "trash"} <= sent
    c = smart_cards(home_client, people["phones"]["Avery"])[0]
    assert c["rule"] == "brain_briefing" and c["title"] == "Rain after lunch" and c["eyebrow"] == "Good morning"
    assert c["items"][0] == {"title": "Weather", "value": "48°, rain at 1"}
    assert smart_cards(home_client, people["phones"]["Theo"]) == []
    assert await brain.run("briefing") == "nothing"  # once a day


async def test_briefing_comes_due_at_seven(hub, brain, monkeypatch):
    fake = brain.use({"title": "Morning", "detail": "", "items": [], "unsure": False})
    hub.smart.weather.data = {"temp": 50}
    seven = datetime(2026, 11, 12, 7, 1, tzinfo=hub.policy.tz)
    monkeypatch.setattr(brain, "now", lambda: seven)
    brain._watch_presence = lambda now: None
    await brain.tick(now=seven.timestamp())
    briefs = [r for r in fake.requests if "morning briefing" in r["system"]]
    assert len(briefs) == 1 and runs(hub, "briefing")[0]["trigger"] == "schedule:at 07:00"
    await brain.tick(now=seven.timestamp() + 60)
    assert len([r for r in fake.requests if "morning briefing" in r["system"]]) == 1


# -- 5. while you were out ------------------------------------------------------------------------------------------
async def test_catch_up_is_for_the_person_who_came_home(hub, brain, home_client, people):
    brain.use({"title": "Two packages came", "detail": "", "items": [{"title": "Door", "value": "2 packages"}],
               "unsure": False})
    pid = people["dylan"]["id"]
    left = time.time() - 3 * 3600
    for i in range(2):
        hub.db.insert("alert_log", {"id": f"al{i}", "ts": time.time() - 3600, "kind": "package",
                                    "title": "Package at the front door", "people": f",{pid},", "payload": "{}"})
    trig = Trigger("hub", "arrival", {"person_id": pid, "away_since": left})
    assert await brain.run(f"catch_up#{pid}", trig) == "suggested"
    assert [c["title"] for c in smart_cards(home_client, people["phones"]["Dylan"])] == ["Two packages came"]
    assert smart_cards(home_client, people["phones"]["Avery"]) == []
    short = Trigger("hub", "arrival", {"person_id": pid, "away_since": time.time() - 600})
    assert await brain.run("catch_up", short) == "nothing"  # ten minutes out isn't "away"


def test_arrival_and_last_left_come_from_presence(hub, brain, people):
    states = hub.home.states
    for name in ("dylan", "avery"):
        states[f"person.{name}"] = {"entity_id": f"person.{name}", "state": "home", "attributes": {}}
    brain._watch_presence(1000.0)
    states["person.dylan"]["state"] = "not_home"
    brain._watch_presence(2000.0)
    assert "left_on" not in brain._pending  # Avery is still home
    states["person.avery"]["state"] = "not_home"
    brain._watch_presence(3000.0)
    assert brain._pending["left_on"][1].name == "last_left"
    states["person.dylan"]["state"] = "home"
    brain._watch_presence(9000.0)
    key = f"catch_up#{people['dylan']['id']}"
    assert brain._pending[key][1].data == {"person_id": people["dylan"]["id"], "away_since": 2000.0}


# -- switches, caps, muting ---------------------------------------------------------------------------------------------
async def test_kill_switch_and_pause(hub, brain, home_client, people):
    brain.use(word_all())
    washer_cycle(hub)
    hub.config.brain.enabled = False
    assert await brain.run("plain_cards") == "off"
    hub.config.brain.enabled = True
    brain.set_paused(True)
    assert await brain.run("plain_cards") == "off"
    brain.set_paused(False)
    assert await brain.run("plain_cards") == "suggested"
    hub.config.brain.enabled = False
    assert smart_cards(home_client, people["phones"]["Avery"]) == []


async def test_observe_logs_but_shows_nothing(hub, brain, home_client, people):
    fake = brain.use(word_all())
    brain.set_job("plain_cards", autonomy="observe")
    washer_cycle(hub)
    assert await brain.run("plain_cards") == "observed"
    assert fake.requests == [] and smart_cards(home_client, people["phones"]["Avery"]) == []
    assert runs(hub, "plain_cards")[0]["action"] == "observe"


async def test_no_cap_by_default_and_an_optional_cap(hub, brain, home_client, people):
    brain.use(word_all(), word_all())
    brain.log_run(job="health", trigger="x", cost_usd=50.0)
    assert not brain.capped()  # unlimited unless a cap is set
    hub.config.brain.monthly_cap_usd = 10.0
    assert brain.capped()
    washer_cycle(hub)
    assert await brain.run("plain_cards") == "observed"
    assert runs(hub, "plain_cards")[0]["outcome"] == "capped"
    assert smart_cards(home_client, people["phones"]["Avery"]) == []


async def test_cap_hit_tells_the_admin_once(hub, brain, people):
    brain.use(word_all(), tokens=(10_000_000, 0))  # one call costs $1
    hub.config.brain.monthly_cap_usd = 0.5
    washer_cycle(hub)
    await brain.run("plain_cards")
    fyi = hub.db.all("SELECT * FROM inbox WHERE group_key='brain.cap'")
    assert len(fyi) == 1 and fyi[0]["person_id"] == people["dylan"]["id"]


async def test_daily_budget_falls_back_to_the_template(hub, brain):
    from hub.config import BrainJobConfig

    fake = brain.use(word_all())
    hub.config.brain.jobs["plain_cards"] = BrainJobConfig(daily_budget_usd=0.001)
    brain.log_run(job="plain_cards", trigger="x", cost_usd=0.01)
    washer_cycle(hub)
    assert await brain.run("plain_cards") == "suggested"
    assert fake.requests == [] and runs(hub, "plain_cards")[-1]["decision"]["fallback"] == "daily_budget"


async def test_a_job_that_keeps_being_put_off_mutes_itself(hub, brain, home_client, people):
    hub.config.brain.mute_after_ignored = 2
    brain.use(word_all(), word_all())
    for n in range(2):
        brain.memory.clear()
        brain._last_hash.clear()
        washer_cycle(hub)
        brain.memory["appliance_done"]["sensor.washer_status"]["name"] = f"Washer {n}"
        await brain.run("plain_cards")
        phone = people["phones"]["Avery" if n == 0 else "Dylan"]  # same card id: each person puts it off once
        c = smart_cards(home_client, phone)[0]
        home_client.post("/v1/smart/dismiss", json={"id": c["id"], "rule": c["rule"], "handled": False},
                         headers=phone.h)
    s = brain.settings(brain.jobs["plain_cards"])
    assert s.autonomy == "observe" and "ignored" in s.muted
    # Turning it back on by hand clears the mute
    r = home_client.post("/v1/brain/jobs/plain_cards", json={"autonomy": "suggest"}, headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200
    assert brain.settings(brain.jobs["plain_cards"]).muted is None


async def test_expired_cards_count_as_ignored(hub, brain):
    brain.use(word_all())
    washer_cycle(hub)
    await brain.run("plain_cards")
    brain._expire_cards(time.time() + 3 * 3600)
    assert runs(hub, "plain_cards")[0]["feedback"] == "ignored" and brain.cards == {}


# -- the assistant's cost, and the app's endpoints ------------------------------------------------------------------------
def test_assistant_turns_are_costed(hub, home_client, people):
    from types import SimpleNamespace

    hub.vault.set("anthropic.api_key", "sk-ant-test")

    async def fake(**kw):
        u = SimpleNamespace(input_tokens=2000, output_tokens=100, cache_read_input_tokens=5000,
                            cache_creation_input_tokens=0)
        return SimpleNamespace(content=[{"type": "text", "text": "Hi"}], stop_reason="end_turn", usage=u)
    hub.assistant.create = fake
    r = home_client.post("/v1/assistant", json={"text": "hello"}, headers=people["phones"]["Avery"].h)
    assert r.status_code == 200, r.text
    [run] = runs(hub, "assistant")
    assert run["model"] == "claude-sonnet-5-5" and run["tokens_cached"] == 5000
    assert run["cost_usd"] == pytest.approx(cost("claude-sonnet-5-5", 2000, 100, 5000))
    s = home_client.get("/v1/brain/summary", headers=people["phones"]["Dylan"].h).json()
    assert s["assistant_usd"] == pytest.approx(run["cost_usd"]) and s["spend_usd"] == 0


async def test_endpoints_are_admin_only(hub, brain, home_client, people):
    brain.use(word_all())
    washer_cycle(hub)
    await brain.run("plain_cards")
    dylan, avery = people["phones"]["Dylan"].h, people["phones"]["Avery"].h
    assert home_client.get("/v1/brain/summary", headers=avery).status_code == 403
    assert home_client.get("/v1/brain/runs", headers=avery).status_code == 403
    s = home_client.get("/v1/brain/summary", headers=dylan).json()
    assert s["status"] == "on" and s["cap_usd"] is None and s["runs"] == 1 and s["cards"] == 1
    assert s["spend_usd"] == pytest.approx(cost("claude-haiku-5-5", 1000, 200))
    assert s["models"] == {"cheap": "claude-haiku-5-5", "smart": "claude-sonnet-5-5"}
    job = next(j for j in s["jobs"] if j["id"] == "plain_cards")
    assert job["autonomy"] == "suggest" and job["runs"] == 1 and job["enabled"]
    listed = home_client.get("/v1/brain/runs", headers=dylan).json()["items"]
    assert listed[0]["job"] == "plain_cards" and listed[0]["headline"] == "Washer's done"
    one = home_client.get(f"/v1/brain/runs/{listed[0]['id']}", headers=dylan).json()
    assert one["decision"]["items"][0]["kind"] == "appliance_done"
    assert home_client.get("/v1/brain/runs/nope", headers=dylan).status_code == 404
    assert home_client.post("/v1/brain/jobs/left_on", json={"autonomy": "act"}, headers=dylan).status_code == 400
    r = home_client.post("/v1/brain/jobs/left_on", json={"enabled": False}, headers=dylan).json()
    assert next(j for j in r["jobs"] if j["id"] == "left_on")["enabled"] is False
    r = home_client.post("/v1/brain", json={"paused": True}, headers=dylan).json()
    assert r["status"] == "paused"


def test_brain_cards_listed_in_suggestion_settings(home_client, people):
    rules = [r["rule"] for r in home_client.get("/v1/smart/settings", headers=people["phones"]["Avery"].h).json()["rules"]]
    assert "brain_briefing" in rules and "brain" not in rules
    kid = [r["rule"] for r in home_client.get("/v1/smart/settings", headers=people["phones"]["Theo"].h).json()["rules"]]
    assert not any(r.startswith("brain") for r in kid)


async def test_a_card_whose_problem_went_away_is_resolved_not_ignored(hub, brain, home_client, people):
    brain.use(word_all())
    empty_room(hub)
    await brain.run("left_on")
    hub.home.states["light.living_room"]["state"] = "off"  # someone turned it off
    assert await brain.run("left_on") == "nothing"
    assert runs(hub, "left_on")[0]["feedback"] == "resolved" and brain.cards == {}
