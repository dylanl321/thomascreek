from __future__ import annotations

import json

import httpx
import respx

from hub.adapters.family import FamilyBoard, parse_ics

TODAY = {
    "date": "2026-11-12",
    "person": {"id": "theo", "name": "Theo"},
    "items": [
        {"id": "bed", "title": "Make bed", "word": "Bed", "kind": "chore", "trackable": True, "completed": True, "points": 5},
        {"id": "teeth", "title": "Brush teeth", "word": "Teeth", "kind": "routine", "trackable": True, "completed": False,
         "points": 2, "time": "07:30", "reported": True},
        {"id": "swim", "title": "Swim lesson", "kind": "schedule", "trackable": False, "completed": False},
    ],
    "progress": {"done": 1, "total": 2, "percent": 50},
    "rewards": {"points": 42, "catalog": [{"id": "game", "title": "Game time", "cost": 25, "emoji": "🎮"}],
                "eligibility": {"game": {"eligible": True}}},
    "streaks": {"bed": {"count": 4, "last_date": "2026-11-12"}, "teeth": {"count": 1}},
}


def test_family_board_today_is_normalized():
    out = FamilyBoard.normalize_today(TODAY)
    assert out["points"] == 42 and out["streak"] == 4 and out["goal"] == 2 and out["done"] == 1
    assert [i["id"] for i in out["items"]] == ["bed", "teeth"]  # schedule rows are not chores
    assert out["items"][1] == {"id": "teeth", "title": "Brush teeth", "icon": "Teeth", "word": "Teeth", "done": False,
                               "points": 2, "time": "07:30", "waiting": True, "locked": False, "kind": "routine"}
    assert out["rewards"] == [{"id": "game", "title": "Game time", "icon": "🎮", "cost": 25, "available": True}]


def test_ics_parse():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:swim-2026-11-12@family-command-center\r\n"
           "SUMMARY:Swim lesson (Theo\\, Juni)\r\nDTSTART;TZID=America/New_York:20261112T163000\r\n"
           "DTEND;TZID=America/New_York:20261112T170000\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:x\r\n"
           "SUMMARY:Pizza night\r\nDTSTART;VALUE=DATE:20261113\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    ev = parse_ics(ics)
    assert ev[0] == {"id": "swim-2026-11-12@family-command-center", "title": "Swim lesson", "start": "2026-11-12T16:30:00",
                     "end": "2026-11-12T17:00:00", "who": "Theo, Juni", "all_day": False, "location": None}
    assert ev[1]["all_day"] is True and ev[1]["start"] == "2026-11-13" and ev[1]["who"] is None


def test_chores_routes_reach_the_board(hub, people, home_client):
    hub.config.family_board.enabled = True
    hub.config.family_board.url = "http://fcc.test"
    hub.db.update("people", "id", people["theo"]["id"], {"family_board_id": "theo"})
    theo, avery = people["phones"]["Theo"], people["phones"]["Avery"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fcc.test/api/today/theo").mock(return_value=httpx.Response(200, json=TODAY))
        done = mock.post("http://fcc.test/api/items/teeth/complete").mock(return_value=httpx.Response(200, json={}))
        r = home_client.get("/v1/chores", headers=theo.h)
        assert r.status_code == 200 and r.json()["person"]["name"] == "Theo" and r.json()["points"] == 42
        # Practice mode first.
        assert home_client.post("/v1/chores/teeth/done", headers=theo.h).json()["dry_run"] is True
        assert not done.called
        hub.config.hub.live_actions = ["chores.done"]
        assert home_client.post("/v1/chores/teeth/done", headers=theo.h).json() == {"ok": True}
        assert json.loads(done.calls[0].request.content) == {"person_id": "theo", "completed": True}
        # Theo can't read someone else's board; an adult can see all of them.
        assert home_client.get(f"/v1/chores?person={people['avery']['id']}", headers=theo.h).status_code == 403
        boards = home_client.get("/v1/chores/all", headers=avery.h).json()["boards"]
        assert [b["person"]["name"] for b in boards] == ["Theo"]


def test_firewalla_link_creates_paused_rule(hub, people, home_client):
    hub.config.firewalla.enabled = True
    hub.config.firewalla.msp_domain = "box.example.invalid"
    hub.vault.set("firewalla.msp_token", "fw-token")
    hub.config.hub.live_actions = ["network.setup", "network.pause"]
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    theo_id = people["theo"]["id"]
    base = "https://box.example.invalid/v2"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/devices").mock(return_value=httpx.Response(200, json=[
            {"mac": "aa:bb:cc:dd:ee:ff", "name": "Theo's iPad", "ip": "192.168.1.70", "online": True, "gid": "box1"}]))
        create = mock.post(f"{base}/rules").mock(return_value=httpx.Response(200, json={"id": "rule-1", "status": "active"}))
        pause = mock.post(f"{base}/rules/rule-1/pause").mock(return_value=httpx.Response(200))
        resume = mock.post(f"{base}/rules/rule-1/resume").mock(return_value=httpx.Response(200))
        mock.get(f"{base}/rules").mock(return_value=httpx.Response(200, json={"results": [{"id": "rule-1", "status": "paused"}]}))

        assert home_client.post(f"/v1/network/kids/{theo_id}/devices", json={"macs": ["AA:BB:CC:DD:EE:FF"]},
                                headers=avery.h).status_code == 403
        r = dylan.secure_post(f"/v1/network/kids/{theo_id}/devices", {"macs": ["aa-bb-cc-dd-ee-ff"]})
        assert r.status_code == 200, r.text
        sent = json.loads(create.calls[0].request.content)
        assert sent["target"] == {"type": "internet"} and sent["scope"] == {"type": "device", "value": "AA:BB:CC:DD:EE:FF"}
        assert sent["action"] == "block" and sent["gid"] == "box1"
        assert create.calls[0].request.headers["authorization"] == "Token fw-token"
        assert pause.called, "a new rule is paused at once so nothing changes for the child"

        kids = home_client.get("/v1/network/kids", headers=avery.h).json()["kids"]
        assert kids[0]["managed"] and kids[0]["paused"] is False and kids[0]["devices"][0]["rule_id"] == "rule-1"

        # Pausing Theo's internet for 30 minutes resumes his block rule (Face ID).
        r = avery.secure_post("/v1/actions", {"action": "network.pause", "target": theo_id, "params": {"minutes": 30}})
        assert r.status_code == 200, r.text
        assert resume.called
        assert hub.timers.pending("network.restore")


def test_activity_category_filter(people, home_client):
    avery = people["phones"]["Avery"]
    avery.secure_post("/v1/actions", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}})
    home_client.post("/v1/actions", json={"action": "light.set", "target": "light.kitchen", "params": {"on": True}},
                     headers=avery.h)
    items = home_client.get("/v1/activity?category=doors", headers=people["phones"]["Dylan"].h).json()["items"]
    assert items and all(i["action"].startswith(("garage.", "lock.")) for i in items)
