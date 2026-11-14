"""One request per tap however the network behaves, and one Home Assistant call for a room of lights."""

from __future__ import annotations

import asyncio

from hub.dedupe import RequestDedupe


def _post(client, person, path, body, rid=None):
    headers = dict(person.h)
    if rid:
        headers["X-TC-Request-Id"] = rid
    return client.post(path, json=body, headers=headers)


def test_the_same_request_id_runs_once(hub, people, home_client):
    hub.config.hub.dry_run = False
    dylan = people["phones"]["Dylan"]
    body = {"action": "light.set", "target": "light.living_room", "params": {"on": True}}
    assert _post(home_client, dylan, "/v1/actions", body, "req-0001-abc").status_code == 200
    assert _post(home_client, dylan, "/v1/actions", body, "req-0001-abc").status_code == 200
    assert len(hub.calls) == 1
    # a new tap is a new id
    assert _post(home_client, dylan, "/v1/actions", body, "req-0002-abc").status_code == 200
    # no id (an older app) always runs
    _post(home_client, dylan, "/v1/actions", body)
    assert len(hub.calls) == 3


async def test_a_repeat_waits_for_the_first_instead_of_running_again():
    d = RequestDedupe()
    runs = []

    async def slow():
        runs.append(1)
        await asyncio.sleep(0.05)
        return {"ok": True}
    a, b = await asyncio.gather(d.run("dev", "abcdefgh1", slow), d.run("dev", "abcdefgh1", slow))
    assert a == b == {"ok": True} and runs == [1]
    # another phone with the same id is its own request
    await d.run("other", "abcdefgh1", slow)
    assert runs == [1, 1]
    d.ttl_s = 0
    await d.run("dev", "abcdefgh2", slow)
    await asyncio.sleep(0.01)
    await d.run("dev", "abcdefgh2", slow)
    assert len(runs) == 4


def test_a_preset_is_one_call_per_setting(hub, people, home_client):
    hub.config.hub.dry_run = False
    dylan = people["phones"]["Dylan"]
    r = _post(home_client, dylan, "/v1/actions/batch", {"action": "light.set", "items": [
        {"target": "light.living_room", "params": {"on": True, "brightness": 40}},
        {"target": "light.kitchen", "params": {"on": True, "brightness": 40}},
        {"target": "light.theo_room", "params": {"on": False}},
    ]})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["targets"]) == ["light.kitchen", "light.living_room", "light.theo_room"]
    assert sorted(hub.calls, key=lambda c: c[1]) == [
        ("light", "turn_off", {"entity_id": "light.theo_room"}),
        ("light", "turn_on", {"brightness_pct": 40, "entity_id": ["light.living_room", "light.kitchen"]}),
    ]


def test_a_batch_skips_what_a_child_may_not_touch(hub, people, home_client):
    hub.config.hub.dry_run = False
    theo = people["phones"]["Theo"]
    r = _post(home_client, theo, "/v1/actions/batch", {"action": "light.set", "items": [
        {"target": "light.theo_room", "params": {"on": True}},
        {"target": "light.kitchen", "params": {"on": True}},
        {"target": "light.nope", "params": {"on": True}},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["targets"] == ["light.theo_room"]
    assert {s["target"] for s in r.json()["skipped"]} == {"light.kitchen", "light.nope"}
    assert hub.calls == [("light", "turn_on", {"entity_id": "light.theo_room"})]


def test_only_simple_actions_batch(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = _post(home_client, dylan, "/v1/actions/batch",
              {"action": "lock.set", "items": [{"target": "lock.front_door", "params": {"locked": False}}]})
    assert r.status_code == 400
