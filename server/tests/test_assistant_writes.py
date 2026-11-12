"""Adding chores and calendar events on the family board, and changing trips through TREK's MCP tools."""

from __future__ import annotations

import json

import httpx
import respx

BOARD = "http://fcc.test"
TREK = "http://trek.test:8097"


def _board(hub, people):
    hub.config.family_board.enabled = True
    hub.config.family_board.url = BOARD
    hub.config.hub.dry_run = False
    hub.db.update("people", "id", people["theo"]["id"], {"family_board_id": "theo"})
    hub.db.update("people", "id", people["avery"]["id"], {"family_board_id": "avery-b"})


ITEMS = {"people": [{"id": "theo", "name": "Theo"}, {"id": "avery-b", "name": "Avery"}], "items": [
    {"id": "tidy-room", "title": "Tidy room", "kind": "chore", "person_ids": ["theo"], "repeat": "daily", "points": 10},
    {"id": "swim", "title": "Swim lesson", "kind": "schedule", "person_ids": ["theo"], "repeat": "weekly",
     "weekdays": ["friday"], "time": "17:00"},
    {"id": "old", "title": "Old", "kind": "chore", "person_ids": ["theo"], "archived": True}]}


def test_adults_add_change_and_remove_chores_and_events(hub, people, home_client):
    _board(hub, people)
    avery, theo = people["phones"]["Avery"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BOARD}/api/bootstrap").mock(return_value=httpx.Response(200, json=ITEMS))
        add = mock.post(f"{BOARD}/api/items").mock(side_effect=lambda req: httpx.Response(
            201, json={"id": "dentist", **json.loads(req.content)}))
        patch = mock.patch(f"{BOARD}/api/items/tidy-room").mock(return_value=httpx.Response(
            200, json={**ITEMS["items"][0], "points": 15}))
        arch = mock.post(f"{BOARD}/api/items/swim/archive").mock(return_value=httpx.Response(200, json={}))
        listed = home_client.get("/v1/family/items", headers=avery.h).json()["items"]
        assert [i["id"] for i in listed] == ["tidy-room", "swim"] and listed[0]["people"] == ["Theo"]
        r = home_client.post("/v1/family/items", headers=avery.h, json={
            "title": "Dentist", "kind": "schedule", "people": ["Theo"], "date": "2026-11-20", "time": "15:30"})
        assert r.status_code == 200, r.text
        sent = json.loads(add.calls[0].request.content)
        assert sent == {"title": "Dentist", "kind": "schedule", "date": "2026-11-20", "time": "15:30",
                        "person_ids": ["theo"]}
        r = home_client.patch("/v1/family/items/tidy-room", headers=avery.h, json={"points": 15})
        assert r.status_code == 200 and json.loads(patch.calls[0].request.content) == {"points": 15}
        assert home_client.delete("/v1/family/items/swim", headers=avery.h).status_code == 200 and arch.called
        # A child can look but not add or change
        assert home_client.post("/v1/family/items", headers=theo.h, json={
            "title": "No bedtime", "kind": "schedule"}).status_code == 403
        assert home_client.patch("/v1/family/items/tidy-room", headers=theo.h, json={"points": 99}).status_code == 403


def test_board_refusals_come_back_readable(hub, people, home_client):
    _board(hub, people)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BOARD}/api/items").mock(return_value=httpx.Response(
            400, json={"error": "Weekly items require at least one weekday"}))
        r = home_client.post("/v1/family/items", headers=people["phones"]["Avery"].h, json={
            "title": "Trash", "kind": "chore", "people": ["all"], "repeat": "weekly"})
        assert r.status_code == 400 and "weekday" in r.json()["message"]


def _trek(hub):
    hub.config.trek.enabled = True
    hub.config.trek.url = TREK
    hub.config.hub.dry_run = False
    hub.vault.set("trek.mcp_client_id", "cid")
    hub.vault.set("trek.mcp_client_secret", "csecret")


def _mcp(mock, calls):
    mock.post(f"{TREK}/oauth/token").mock(return_value=httpx.Response(
        200, json={"access_token": "trekoa_x", "expires_in": 3600}))

    def handle(req):
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer trekoa_x"
        if body["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s1"},
                                  json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}})
        if "id" not in body:
            return httpx.Response(202)
        assert req.headers["mcp-session-id"] == "s1"
        if body["method"] == "tools/list":
            tools = [{"name": n, "description": n, "inputSchema": {"type": "object"}} for n in
                     ("create_trip", "create_and_assign_place", "delete_place", "add_trip_member", "list_trips")]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": tools}})
        name = body["params"]["name"]
        if name == "create_trip" and not body["params"]["arguments"].get("title"):
            res = {"isError": True, "content": [{"type": "text", "text": "title is required"}]}
        else:
            res = {"content": [{"type": "text", "text": json.dumps({"id": 7, "tool": name})}]}
        # TREK answers tool calls as server-sent events
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': res})}\n\n")
    mock.post(f"{TREK}/mcp").mock(side_effect=handle)


def test_trips_change_through_treks_tools(hub, people, home_client):
    _trek(hub)
    avery, theo = people["phones"]["Avery"], people["phones"]["Theo"]
    calls: list = []
    with respx.mock(assert_all_called=False) as mock:
        _mcp(mock, calls)
        tools = home_client.get("/v1/trip-tools", headers=avery.h).json()["tools"]
        names = {t["name"]: t for t in tools}
        assert set(names) == {"create_trip", "create_and_assign_place", "delete_place", "list_trips"}
        assert names["delete_place"]["removes"] is True
        r = home_client.post("/v1/trip-tools/create_trip", headers=avery.h,
                             json={"arguments": {"title": "Beach", "start_date": "2027-06-01"}})
        assert r.status_code == 200, r.text and r.json()["result"] == {"id": 7, "tool": "create_trip"}
        assert r.json()["result"] == {"id": 7, "tool": "create_trip"}
        bad = home_client.post("/v1/trip-tools/create_trip", headers=avery.h, json={"arguments": {}})
        assert bad.status_code == 400 and "title is required" in bad.json()["message"]
        # Removing needs DELETE (so the app and the assistant ask first); members and sharing aren't offered
        assert home_client.post("/v1/trip-tools/delete_place", headers=avery.h, json={}).status_code == 400
        assert home_client.request("DELETE", "/v1/trip-tools/delete_place", headers=avery.h,
                                   json={"arguments": {"place_id": 3}}).status_code == 200
        assert home_client.post("/v1/trip-tools/add_trip_member", headers=avery.h, json={}).status_code == 404
        # Children can't plan
        assert home_client.post("/v1/trip-tools/create_trip", headers=theo.h,
                                json={"arguments": {"title": "x"}}).status_code == 403
    assert sum(1 for c in calls if c["method"] == "initialize") == 1  # one session reused


def test_trips_without_a_machine_client_say_how_to_set_it_up(hub, people, home_client):
    _trek(hub)
    hub.vault.delete("trek.mcp_client_secret")
    r = home_client.get("/v1/trip-tools", headers=people["phones"]["Avery"].h)
    assert r.status_code == 503 and "admin page" in r.json()["message"]


def test_board_without_the_parent_pin_says_where_to_save_it(hub, people, home_client):
    _board(hub, people)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BOARD}/api/bootstrap").mock(return_value=httpx.Response(200, json=ITEMS))
        mock.post(f"{BOARD}/api/items").mock(return_value=httpx.Response(401, json={"error": "Admin token required"}))
        r = home_client.post("/v1/family/items", headers=people["phones"]["Avery"].h, json={
            "title": "test chore", "kind": "chore", "people": ["Theo"]})
        assert r.status_code == 503 and "parent PIN" in r.json()["message"] and "admin page" in r.json()["message"]
