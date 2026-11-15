"""Notification audiences: technical ones reach admins only, household ones the household, personal ones the person.
Free-text alerts are sorted by AI; urgent ones never wait for it."""

from __future__ import annotations

import asyncio

from hub.brain.models import Completion
from hub.notify_sort import by_rules, signature

from .test_admin_page import _sign_in


def _inbox(hub, pid):
    return [r["title"] for r in hub.db.all("SELECT title FROM inbox WHERE person_id=? AND cat != 'devices' ORDER BY ts",
                                           [pid])]


def _get(ph, path):
    return ph.client.get(path, headers=ph.h)


def _put(ph, path, body):
    return ph.client.put(path, json=body, headers=ph.h)


def _post(ph, path, body):
    return ph.client.post(path, json=body, headers=ph.h)


class FakeModel:
    def __init__(self, answer: str = "systems", delay: float = 0.0):
        self.answer, self.delay, self.calls = answer, delay, []

    async def complete(self, *, model, system, prompt, schema, max_tokens, effort):
        self.calls.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return Completion(text='{"category": "%s", "reason": "test", "unsure": false}' % self.answer, model=model)


def _ai(hub, model: FakeModel) -> FakeModel:
    hub.vault.set(hub.config.brain.key_secret, "sk-test")
    hub.brain.router.provider = model
    return model


async def test_technical_reaches_admins_only(hub, people):
    everyone = ("admin", "adult", "child")
    await hub.notify.send(title="Thomas Creek update ready", body="In TestFlight", cat="updates", roles=everyone)
    await hub.notify.send(title="Storage pool degraded", body=None, cat="systems", roles=everyone)
    await hub.notify.send(title="Rain tonight", body=None, cat="weather", roles=everyone)
    assert _inbox(hub, people["dylan"]["id"]) == ["Thomas Creek update ready", "Storage pool degraded", "Rain tonight"]
    assert _inbox(hub, people["avery"]["id"]) == ["Rain tonight"]
    assert _inbox(hub, people["theo"]["id"]) == ["Rain tonight"]


async def test_role_defaults_and_overrides(hub, people):
    avery, theo = people["avery"], people["theo"]
    assert hub.notify.prefs(avery)["cameras"] and not hub.notify.prefs(theo)["cameras"]  # kids: no camera alerts
    # A grown-up can't give themselves technical alerts; an admin can.
    assert not hub.notify.set_prefs(avery["id"], {"systems": True}, by_admin=False)["systems"]
    assert hub.notify.set_prefs(avery["id"], {"systems": True, "media": False}, by_admin=True)["systems"]
    await hub.notify.send(title="App stopped", body=None, cat="systems", roles=("admin", "adult"))
    await hub.notify.send(title="Dune is ready", body=None, cat="media", person_ids=[avery["id"]])
    assert _inbox(hub, avery["id"]) == ["App stopped"]
    # Turning one back to the default stores nothing for it.
    hub.notify.set_prefs(avery["id"], {"media": True}, by_admin=False)
    assert "media" not in hub.db.kv_get(f"notify.cats.{avery['id']}")


async def test_old_device_prefs_carry_over(hub, people):
    avery = people["avery"]
    dev = people["phones"]["Avery"].device_id
    hub.db.update("devices", "id", dev, {"prefs": {"kitchen": False, "media": True}})
    p = hub.notify.prefs(avery)
    assert p["kitchen"] is False and p["media"] is True


async def test_rules_without_ai(hub, people):
    assert by_rules("house", "TrueNAS: pool tank is degraded", None) == "systems"
    assert by_rules("house", "12 devices not responding", None) == "systems"
    assert by_rules("house", "Someone at the door", "Breezeway") == "cameras"
    assert by_rules("house", "Freezer is warm", "15°F") == "house"
    assert signature("house", "12 devices not responding") == signature("house", "88 devices not responding")
    # No key: rules decide, and the NAS alert reaches Dylan only.
    await hub.notify.send(title="TrueNAS: pool tank is degraded", body=None, cat="house", roles=("admin", "adult"))
    assert _inbox(hub, people["avery"]["id"]) == [] and _inbox(hub, people["dylan"]["id"])
    assert hub.notify.sorter.recent()[0]["how"] == "rules"


async def test_ai_sorts_and_caches(hub, people):
    model = _ai(hub, FakeModel("systems"))
    for n in (3, 5):
        await hub.notify.send(title=f"{n} lights unreachable", body=None, cat="house", roles=("admin", "adult"))
    assert len(model.calls) == 1  # the second one of the same kind came from the cache
    assert _inbox(hub, people["avery"]["id"]) == []
    assert len(_inbox(hub, people["dylan"]["id"])) == 2
    assert hub.notify.sorter.recent()[0]["how"] == "ai"


async def test_known_categories_never_ask_the_ai(hub, people):
    model = _ai(hub, FakeModel("systems"))
    await hub.notify.send(title="Someone is at the door", body=None, cat="cameras", level="act_now",
                          roles=("admin", "adult"))
    assert model.calls == [] and _inbox(hub, people["avery"]["id"]) == ["Someone is at the door"]


async def test_urgent_never_waits_for_the_ai(hub, people):
    model = _ai(hub, FakeModel("systems", delay=5))
    loop = asyncio.get_running_loop()
    start = loop.time()
    await hub.notify.send(title="Water leak in the basement", body=None, cat="house", level="act_now",
                          roles=("admin", "adult"))
    assert loop.time() - start < 1
    assert _inbox(hub, people["avery"]["id"]) == ["Water leak in the basement"]
    assert hub.notify.sorter.recent()[0]["how"] == "instant"
    await asyncio.sleep(0.01)
    assert model.calls == []  # urgent notices never reach the AI, not even in the background


async def test_doorbell_from_automation_never_reaches_the_ai(hub, people):
    model = _ai(hub, FakeModel("systems", delay=5))
    loop = asyncio.get_running_loop()
    start = loop.time()
    await hub.notify.send(title="Doorbell", body="Someone is at the front door", cat="auto",
                          roles=("admin", "adult"))
    assert loop.time() - start < 1
    assert _inbox(hub, people["avery"]["id"]) == ["Doorbell"]
    row = hub.notify.sorter.recent()[0]
    assert row["how"] == "instant" and row["cat"] == "cameras"
    await asyncio.sleep(0.01)
    assert model.calls == []


async def test_slow_ai_falls_back_to_rules(hub, people, monkeypatch):
    import hub.notify_sort as ns

    monkeypatch.setattr(ns, "AI_TIMEOUT_S", 0.05)
    _ai(hub, FakeModel("house", delay=1))
    await hub.notify.send(title="Backup failed", body="nightly", cat="house", roles=("admin", "adult"))
    assert _inbox(hub, people["avery"]["id"]) == []
    assert hub.notify.sorter.recent()[0]["how"] == "rules"


def test_automation_named_kids_but_technical(hub, people, home_client):
    hub.vault.set("alerts.webhook_token", "tok123")
    for p in ("avery", "theo"):
        hub.alerts.set_prefs(people[p]["id"], {"on": True})
    body = {"title": "Zigbee coordinator offline", "message": "Home Assistant", "to": ["everyone"]}
    r = home_client.post("/v1/alerts/notify", json=body, headers={"Authorization": "Bearer tok123"})
    assert r.json()["ok"]
    assert _inbox(hub, people["avery"]["id"]) == [] and _inbox(hub, people["theo"]["id"]) == []


def test_app_categories_and_prefs(hub, people):
    avery, dylan = people["phones"]["Avery"], people["phones"]["Dylan"]
    r = _get(avery, "/v1/alerts").json()
    groups = {c["id"]: c["group"] for c in r["categories"]}
    assert "systems" not in groups and groups["media"] == "personal" and groups["weather"] == "household"
    assert r["notify"] == r["device_prefs"]
    r = _put(avery, "/v1/alerts", {"notify": {"media": False, "systems": True}}).json()
    assert r["notify"]["media"] is False and r["notify"]["systems"] is False
    # Same person on another device sees the same thing (people prefs, not device prefs).
    assert hub.notify.prefs(people["avery"])["media"] is False
    r = _get(dylan, "/v1/alerts").json()
    assert {c["id"] for c in r["categories"]} >= {"systems", "updates", "devices", "feedback"}
    # Older apps: POST /v1/devices/prefs changes the person too.
    out = _post(avery, "/v1/devices/prefs", {"prefs": {"weather": False}}).json()
    assert out["prefs"]["weather"] is False and hub.notify.prefs(people["avery"])["weather"] is False


def test_admin_page_sets_a_persons_notifications(hub, people, home_client):
    csrf = _sign_in(people, home_client)
    page = home_client.get("/admin/people").text
    assert "Notifications" in page and "Technical (admins)" in page
    avery = people["avery"]
    r = home_client.post("/admin/people/notify", data={"csrf": csrf, "person": avery["id"], "on": ["weather", "systems"]})
    assert "Saved" in r.text
    p = hub.notify.prefs(avery)
    assert p["systems"] and p["weather"] and not p["cameras"] and not p["media"]
    r = home_client.post("/admin/people/notify", data={"csrf": csrf, "person": avery["id"], "reset": "1"})
    assert hub.notify.prefs(avery)["cameras"] and not hub.notify.prefs(avery)["systems"]


async def test_admin_fix_for_a_kind_of_alert(hub, people, home_client):
    csrf = _sign_in(people, home_client)
    await hub.notify.send(title="Laundry is done", body=None, cat="house", roles=("admin", "adult"))
    row = hub.notify.sorter.recent()[0]
    assert "How alerts were sorted" in home_client.get("/admin/people").text
    home_client.post("/admin/people/sorted", data={"csrf": csrf, "sig": row["sig"], "cat": "systems"})
    await hub.notify.send(title="Laundry is done", body=None, cat="house", roles=("admin", "adult"))
    assert _inbox(hub, people["avery"]["id"]) == ["Laundry is done"]  # only the first one
    assert hub.notify.sorter.recent()[0]["how"] == "admin"
