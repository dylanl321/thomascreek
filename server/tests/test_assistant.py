"""Ask Thomas Creek: tools run as the caller, Face ID and deletes become confirm cards."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


class FakeModel:
    """Plays back a script: each step is a list of tool calls, or a final text."""

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> Any:
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        step = self.script.pop(0)
        if isinstance(step, str):
            return SimpleNamespace(content=[{"type": "text", "text": step}], stop_reason="end_turn", usage=None)
        blocks = [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(step)]
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=None)

    def results(self, i: int) -> list[dict[str, Any]]:
        msgs = self.requests[i]["messages"]
        return [json.loads(b["content"]) for b in msgs[-1]["content"]]


@pytest.fixture
def ai(hub):
    hub.vault.set("anthropic.api_key", "sk-ant-test")
    hub.config.hub.dry_run = False

    def use(script):
        fake = FakeModel(script)
        hub.assistant.create = fake
        return fake
    return use


def ask(client, phone, text, **kw):
    return client.post("/v1/assistant", json={"text": text, **kw}, headers=phone.h)


def test_needs_a_key(hub, home_client, people):
    r = ask(home_client, people["phones"]["Avery"], "lights off")
    assert r.status_code == 503 and "admin page" in r.json()["message"]
    assert home_client.get("/v1/assistant", headers=people["phones"]["Avery"].h).json() == {"ready": False}


def test_adult_turns_off_a_light(hub, home_client, people, ai):
    fake = ai([
        [("house", {"query": "kitchen"})],
        [("act", {"action": "light.set", "target": "light.kitchen", "params": {"on": False}, "summary": "Kitchen off"})],
        "Kitchen light is off.",
    ])
    r = ask(home_client, people["phones"]["Avery"], "turn off the kitchen light", screen="Home")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Kitchen light is off." and body["changed"] is True
    assert body["steps"] == [{"label": "Kitchen off", "status": "done"}]
    assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in hub.calls
    house = fake.results(1)[0]
    assert [d["id"] for d in house["devices"]] == ["light.kitchen"]
    # Who is asking rides in a system message after the question, not the cached system prompt
    sysmsg = fake.requests[0]["messages"][1]
    assert sysmsg["role"] == "system" and "Avery" in sysmsg["content"] and "Home" in sysmsg["content"]
    assert "Avery" not in json.dumps(fake.requests[0]["system"])
    row = hub.db.execute("SELECT source FROM activity WHERE action='light.set' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row[0] == "ask"
    # The conversation continues
    ai(["Sure."])
    r2 = ask(home_client, people["phones"]["Avery"], "thanks", conversation_id=body["conversation_id"])
    assert r2.json()["conversation_id"] == body["conversation_id"]


def test_garage_needs_face_id_card(hub, home_client, people, ai):
    fake = ai([
        [("act", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True},
                  "summary": "Open the large garage door"})],
        "Confirm in the card and it'll open.",
    ])
    body = ask(home_client, people["phones"]["Dylan"], "open the garage").json()
    assert hub.calls == []
    assert fake.results(1)[0]["waiting"] is True
    (card,) = body["confirms"]
    assert card["kind"] == "faceid" and card["method"] == "POST" and card["path"] == "/v1/actions"
    assert card["body"] == {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}}
    # The app replays it with Face ID, then tells the assistant
    phone = people["phones"]["Dylan"]
    assert phone.secure_post(card["path"], card["body"]).status_code == 200
    assert ("cover", "open_cover", {"entity_id": "cover.large_garage"}) in hub.calls
    fake2 = ai(["It's open."])
    ask(home_client, phone, "is it open?", conversation_id=body["conversation_id"],
        confirmed=[{"id": card["id"], "ok": True}])
    assert "Open the large garage door: done" in fake2.requests[0]["messages"][-2]["content"]


def test_child_is_refused_and_sees_less(hub, home_client, people, ai):
    fake = ai([
        [("house", {}), ("act", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True},
                                 "summary": "Open garage"}),
         ("hub_get", {"path": "/v1/cameras"})],
        "Garages are for grown-ups.",
    ])
    body = ask(home_client, people["phones"]["Theo"], "open the garage and show cameras").json()
    house, act, cams = fake.results(1)
    ids = {d["id"] for d in house["devices"]}
    assert "cover.large_garage" not in ids and "camera.driveway" not in ids and "light.theo_room" in ids
    assert act["error"] and cams["error"]
    assert hub.calls == [] and body["confirms"] == []
    assert body["steps"] == [{"label": "Open garage", "status": "denied"}]
    tools = {t["name"]: t for t in fake.requests[0]["tools"]}
    assert "/v1/cameras" not in tools["hub_get"]["description"]
    assert "/v1/chores" in tools["hub_get"]["description"]


def test_deletes_wait_and_private_routes_are_closed(hub, home_client, people, ai):
    fake = ai([
        [("hub_change", {"method": "DELETE", "path": "/v1/meals/recipes/tacos", "summary": "Delete Tacos"}),
         ("hub_get", {"path": "/v1/vpn/pool"}), ("hub_get", {"path": "/v1/admin/web-link"}),
         ("hub_get", {"path": "v1/chores"})],
        "Tap confirm to delete it.",
    ])
    body = ask(home_client, people["phones"]["Dylan"], "delete the tacos recipe").json()
    dele, vpn, admin, chores = fake.results(1)
    assert dele["waiting"] and body["confirms"][0]["kind"] == "tap"
    assert "isn't available" in vpn["error"] and "isn't available" in admin["error"]
    assert "error" not in chores or "403" not in chores.get("error", "")
    desc = next(t for t in fake.requests[0]["tools"] if t["name"] == "hub_get")["description"]
    assert "/v1/vpn" not in desc and "/v1/admin" not in desc and "/v1/auth" not in desc
